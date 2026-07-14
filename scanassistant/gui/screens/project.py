"""Project screen, preparation mode.

Tabs: Summary, Folders, Capture, Framing, Exports, Metadata, CSV, Log.
Every setting change applies immediately (no global Save button): each
field self-validates on focus loss/toggle, writes `campaign.json`
(atomically), and logs `PROJECT/setting_changed {key, before, after}`.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QSignalBlocker, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from scanassistant.gui.errors import format_business_error
from scanassistant.gui.widgets.csv_table import CsvTableWidget
from scanassistant.gui.widgets.log_table import LogTableWidget
from scanassistant.i18n import t
from scanassistant.journal.journal import Journal
from scanassistant.project.campaign import Campaign, save_campaign
from scanassistant.project.errors import InvalidCampaignError
from scanassistant.project.inventory import (
    STATUS_COLUMN,
    Inventory,
    export_inventory,
    load_inventory,
)
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.state import ProjectState


class ProjectScreen(QWidget):
    cursor_change_requested = Signal(str)  # « Set cursor here » (06 §6)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.campaign: Campaign | None = None
        self.state: ProjectState | None = None
        self.inventory: Inventory | None = None
        self.paths: CampaignPaths | None = None
        self.journal: Journal | None = None

        self._tabs = QTabWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs)

        self._tabs.addTab(self._build_summary_tab(), t("project.tab_summary"))
        self._tabs.addTab(self._build_folders_tab(), t("project.tab_folders"))
        self._tabs.addTab(self._build_capture_tab(), t("project.tab_capture"))
        self._tabs.addTab(self._build_framing_tab(), t("project.tab_framing"))
        self._tabs.addTab(self._build_exports_tab(), t("project.tab_exports"))
        self._tabs.addTab(self._build_metadata_tab(), t("project.tab_metadata"))
        self._tabs.addTab(self._build_csv_tab(), t("project.tab_csv"))
        self._tabs.addTab(self._build_log_tab(), t("project.tab_log"))

    # --- chargement ---------------------------------------------------------

    def load(
        self,
        *,
        campaign: Campaign,
        state: ProjectState,
        inventory: Inventory,
        paths: CampaignPaths,
        journal: Journal,
    ) -> None:
        self.campaign = campaign
        self.state = state
        self.inventory = inventory
        self.paths = paths
        self.journal = journal
        self._refresh_all()

    def refresh(self) -> None:
        """Resynchronise l'affichage (ex. retour du mode capture, M4)."""
        self._refresh_all()

    def _refresh_all(self) -> None:
        self._refresh_summary()
        self._refresh_folders()
        self._refresh_capture()
        self._refresh_framing()
        self._refresh_exports()
        self._refresh_metadata()
        self._refresh_csv()
        self._refresh_log()

    # --- immediate persistence ------------------------------------------------

    def _commit(self, key: str, before: object, after: object) -> None:
        assert self.campaign is not None
        assert self.paths is not None
        assert self.journal is not None
        try:
            save_campaign(self.campaign, self.paths.campaign_json)
        except InvalidCampaignError as exc:
            QMessageBox.warning(
                self, t("project.invalid_setting_title"), format_business_error(exc)
            )
            return
        self.journal.log(
            "PROJECT", "setting_changed", details={"key": key, "before": before, "after": after}
        )

    # --- Summary (+ identity, "Campaign settings…") -----------------------

    def _build_summary_tab(self) -> QWidget:
        self._summary_name_label = QLabel()
        self._summary_counts_label = QLabel()
        self._summary_paths_label = QLabel()
        self._summary_paths_label.setWordWrap(True)
        self._summary_paths_label.setProperty("role", "secondary")

        self.description_edit = QLineEdit()
        self.description_edit.editingFinished.connect(
            lambda: self._commit_identity_field("description", self.description_edit.text().strip())
        )
        self.operator_edit = QLineEdit()
        self.operator_edit.editingFinished.connect(
            lambda: self._commit_identity_field("operator", self.operator_edit.text().strip())
        )
        self.institution_edit = QLineEdit()
        self.institution_edit.editingFinished.connect(
            lambda: self._commit_identity_field("institution", self.institution_edit.text().strip())
        )
        self.negative_format_edit = QLineEdit()
        self.negative_format_edit.editingFinished.connect(
            lambda: self._commit_identity_field(
                "negative_format", self.negative_format_edit.text().strip()
            )
        )
        identity_form = QFormLayout()
        identity_form.addRow(t("wizard.step1.description"), self.description_edit)
        identity_form.addRow(t("wizard.step1.operator"), self.operator_edit)
        identity_form.addRow(t("wizard.step1.institution"), self.institution_edit)
        identity_form.addRow(t("wizard.step1.negative_format"), self.negative_format_edit)

        layout = QVBoxLayout()
        layout.addWidget(self._summary_name_label)
        layout.addWidget(self._summary_counts_label)
        layout.addWidget(self._summary_paths_label)
        layout.addSpacing(16)
        layout.addLayout(identity_form)
        layout.addStretch(1)
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _refresh_summary(self) -> None:
        if self.campaign is None or self.inventory is None or self.paths is None:
            return
        self._summary_name_label.setText(self.campaign.name)
        self._summary_name_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        total = len(self.inventory.rows)
        done = sum(1 for row in self.inventory.rows if row[STATUS_COLUMN] == "done")
        self._summary_counts_label.setText(
            t("project.summary_counts", done=done, total=total, remaining=total - done)
        )
        self._summary_paths_label.setText(t("project.summary_root", root=str(self.paths.root)))

        widgets = (
            self.description_edit,
            self.operator_edit,
            self.institution_edit,
            self.negative_format_edit,
        )
        with ExitStack() as stack:
            for w in widgets:
                stack.enter_context(QSignalBlocker(w))
            self.description_edit.setText(self.campaign.description)
            self.operator_edit.setText(self.campaign.operator)
            self.institution_edit.setText(self.campaign.institution)
            self.negative_format_edit.setText(self.campaign.negative_format)

    def show_settings_tab(self) -> None:
        """Project ▸ Campaign settings… (06 §12)."""
        self._tabs.setCurrentIndex(0)
        self.description_edit.setFocus()

    def _commit_identity_field(self, attr: str, after: str) -> None:
        if self.campaign is None:
            return
        before = getattr(self.campaign, attr)
        if after == before:
            return
        setattr(self.campaign, attr, after)
        self._commit(attr, before, after)

    # --- Folders --------------------------------------------------------

    def _build_folders_tab(self) -> QWidget:
        self.watched_folder_edit = QLineEdit()
        self.watched_folder_edit.editingFinished.connect(self._on_watched_folder_changed)
        browse = QPushButton(t("wizard.browse"))
        browse.clicked.connect(self._browse_watched_folder)
        watched_row = QHBoxLayout()
        watched_row.addWidget(self.watched_folder_edit, 1)
        watched_row.addWidget(browse)

        open_folder_button = QPushButton(t("project.open_campaign_folder"))
        open_folder_button.clicked.connect(self._open_campaign_folder)

        form = QFormLayout()
        form.addRow(t("wizard.step2.watched_folder"), watched_row)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(open_folder_button)
        layout.addStretch(1)
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _refresh_folders(self) -> None:
        if self.campaign is None:
            return
        with QSignalBlocker(self.watched_folder_edit):
            self.watched_folder_edit.setText(self.campaign.capture.watched_folder)

    def _browse_watched_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, t("wizard.step2.watched_folder"))
        if path:
            self.watched_folder_edit.setText(path)
            self._on_watched_folder_changed()

    def _on_watched_folder_changed(self) -> None:
        if self.campaign is None:
            return
        before = self.campaign.capture.watched_folder
        after = self.watched_folder_edit.text().strip()
        if after == before:
            return
        self.campaign.capture.watched_folder = after
        self._commit("capture.watched_folder", before, after)

    def _open_campaign_folder(self) -> None:
        if self.paths is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.root)))

    # --- Capture --------------------------------------------------------

    def _build_capture_tab(self) -> QWidget:
        self.extensions_edit = QLineEdit()
        self.extensions_edit.editingFinished.connect(self._on_extensions_changed)

        self.watch_mode_combo = QComboBox()
        self.watch_mode_combo.addItem(t("project.watch_mode_auto"), "auto")
        self.watch_mode_combo.addItem(t("project.watch_mode_native"), "native")
        self.watch_mode_combo.addItem(t("project.watch_mode_polling"), "polling")
        self.watch_mode_combo.currentIndexChanged.connect(self._on_watch_mode_changed)

        self.stabilization_delay_spin = QDoubleSpinBox()
        self.stabilization_delay_spin.setRange(0.5, 30)
        self.stabilization_delay_spin.setSuffix(" s")
        self.stabilization_delay_spin.editingFinished.connect(self._on_stabilization_delay_changed)

        self.stabilization_timeout_spin = QSpinBox()
        self.stabilization_timeout_spin.setRange(10, 3600)
        self.stabilization_timeout_spin.setSuffix(" s")
        self.stabilization_timeout_spin.editingFinished.connect(
            self._on_stabilization_timeout_changed
        )

        self.verify_checksum_check = QCheckBox(t("project.verify_checksum"))
        self.verify_checksum_check.toggled.connect(self._on_verify_checksum_changed)

        form = QFormLayout()
        form.addRow(t("project.extensions"), self.extensions_edit)
        form.addRow(t("project.watch_mode"), self.watch_mode_combo)
        form.addRow(t("project.stabilization_delay"), self.stabilization_delay_spin)
        form.addRow(t("project.stabilization_timeout"), self.stabilization_timeout_spin)
        form.addRow(self.verify_checksum_check)

        widget = QWidget()
        widget.setLayout(form)
        return widget

    def _refresh_capture(self) -> None:
        if self.campaign is None:
            return
        c = self.campaign.capture
        with ExitStack() as stack:
            for w in (
                self.extensions_edit,
                self.watch_mode_combo,
                self.stabilization_delay_spin,
                self.stabilization_timeout_spin,
                self.verify_checksum_check,
            ):
                stack.enter_context(QSignalBlocker(w))
            self.extensions_edit.setText(", ".join(c.extensions))
            self.watch_mode_combo.setCurrentIndex(self.watch_mode_combo.findData(c.watch_mode))
            self.stabilization_delay_spin.setValue(c.stabilization_delay_s)
            self.stabilization_timeout_spin.setValue(c.stabilization_timeout_s)
            self.verify_checksum_check.setChecked(c.verify_checksum)

    def _on_extensions_changed(self) -> None:
        if self.campaign is None:
            return
        before = self.campaign.capture.extensions
        after = [e.strip() for e in self.extensions_edit.text().split(",") if e.strip()]
        if after == before:
            return
        self.campaign.capture.extensions = after
        self._commit("capture.extensions", before, after)

    def _on_watch_mode_changed(self, _index: int) -> None:
        if self.campaign is None:
            return
        before = self.campaign.capture.watch_mode
        after = self.watch_mode_combo.currentData()
        if after == before:
            return
        self.campaign.capture.watch_mode = after
        self._commit("capture.watch_mode", before, after)

    def _on_stabilization_delay_changed(self) -> None:
        if self.campaign is None:
            return
        before = self.campaign.capture.stabilization_delay_s
        after = self.stabilization_delay_spin.value()
        if after == before:
            return
        self.campaign.capture.stabilization_delay_s = after
        self._commit("capture.stabilization_delay_s", before, after)

    def _on_stabilization_timeout_changed(self) -> None:
        if self.campaign is None:
            return
        before = self.campaign.capture.stabilization_timeout_s
        after = self.stabilization_timeout_spin.value()
        if after == before:
            return
        self.campaign.capture.stabilization_timeout_s = after
        self._commit("capture.stabilization_timeout_s", before, after)

    def _on_verify_checksum_changed(self, checked: bool) -> None:
        if self.campaign is None:
            return
        before = self.campaign.capture.verify_checksum
        after = bool(checked)
        if after == before:
            return
        self.campaign.capture.verify_checksum = after
        self._commit("capture.verify_checksum", before, after)

    # --- Framing --------------------------------------------------------

    def _build_framing_tab(self) -> QWidget:
        self.framing_enabled_check = QCheckBox(t("wizard.step4.framing_enabled"))
        self.framing_enabled_check.toggled.connect(self._on_framing_enabled_changed)

        self.orientation_combo = QComboBox()
        self.orientation_combo.addItem(t("common.horizontal"), "horizontal")
        self.orientation_combo.addItem(t("common.vertical"), "vertical")
        self.orientation_combo.currentIndexChanged.connect(self._on_orientation_changed)

        self.size_mode_combo = QComboBox()
        self.size_mode_combo.addItem(t("wizard.step4.size_mode_native"), "native")
        self.size_mode_combo.addItem(t("wizard.step4.size_mode_fixed"), "fixed")
        self.size_mode_combo.currentIndexChanged.connect(self._on_size_mode_changed)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(512, 20000)
        self.width_spin.editingFinished.connect(self._on_dimensions_changed)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(512, 20000)
        self.height_spin.editingFinished.connect(self._on_dimensions_changed)

        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0, 20)
        self.margin_spin.setSuffix(" %")
        self.margin_spin.editingFinished.connect(self._on_margin_changed)

        form = QFormLayout()
        form.addRow(self.framing_enabled_check)
        form.addRow(t("wizard.step4.default_orientation"), self.orientation_combo)
        form.addRow(t("wizard.step4.size_mode"), self.size_mode_combo)
        form.addRow(t("wizard.step4.final_width"), self.width_spin)
        form.addRow(t("wizard.step4.final_height"), self.height_spin)
        form.addRow(t("wizard.step4.margin_pct"), self.margin_spin)

        widget = QWidget()
        widget.setLayout(form)
        return widget

    def _refresh_framing(self) -> None:
        if self.campaign is None:
            return
        f = self.campaign.framing
        with ExitStack() as stack:
            for w in (
                self.framing_enabled_check,
                self.orientation_combo,
                self.size_mode_combo,
                self.width_spin,
                self.height_spin,
                self.margin_spin,
            ):
                stack.enter_context(QSignalBlocker(w))
            self.framing_enabled_check.setChecked(f.enabled)
            self.orientation_combo.setCurrentIndex(
                self.orientation_combo.findData(f.default_orientation)
            )
            self.size_mode_combo.setCurrentIndex(self.size_mode_combo.findData(f.size_mode))
            self.width_spin.setValue(f.final_dimensions_px[0])
            self.height_spin.setValue(f.final_dimensions_px[1])
            self.margin_spin.setValue(f.margin_pct)
        self.width_spin.setEnabled(f.size_mode == "fixed")
        self.height_spin.setEnabled(f.size_mode == "fixed")

    def _on_framing_enabled_changed(self, checked: bool) -> None:
        if self.campaign is None:
            return
        before = self.campaign.framing.enabled
        after = bool(checked)
        if after == before:
            return
        self.campaign.framing.enabled = after
        self._commit("framing.enabled", before, after)

    def _on_orientation_changed(self, _index: int) -> None:
        if self.campaign is None:
            return
        before = self.campaign.framing.default_orientation
        after = self.orientation_combo.currentData()
        if after == before:
            return
        self.campaign.framing.default_orientation = after
        self._commit("framing.default_orientation", before, after)

    def _on_size_mode_changed(self, _index: int) -> None:
        if self.campaign is None:
            return
        before = self.campaign.framing.size_mode
        after = self.size_mode_combo.currentData()
        self.width_spin.setEnabled(after == "fixed")
        self.height_spin.setEnabled(after == "fixed")
        if after == before:
            return
        self.campaign.framing.size_mode = after
        self._commit("framing.size_mode", before, after)

    def _on_dimensions_changed(self) -> None:
        if self.campaign is None:
            return
        before = list(self.campaign.framing.final_dimensions_px)
        after = [self.width_spin.value(), self.height_spin.value()]
        if after == before:
            return
        self.campaign.framing.final_dimensions_px = after
        self._commit("framing.final_dimensions_px", before, after)

    def _on_margin_changed(self) -> None:
        if self.campaign is None:
            return
        before = self.campaign.framing.margin_pct
        after = self.margin_spin.value()
        if after == before:
            return
        self.campaign.framing.margin_pct = after
        self._commit("framing.margin_pct", before, after)

    # --- Exports --------------------------------------------------------

    def _build_exports_tab(self) -> QWidget:
        self.tiff_enabled_check = QCheckBox(t("wizard.step5.enabled"))
        self.tiff_enabled_check.toggled.connect(self._on_tiff_enabled_changed)
        self.tiff_bits_combo = QComboBox()
        self.tiff_bits_combo.addItems(["8", "16"])
        self.tiff_bits_combo.currentIndexChanged.connect(self._on_tiff_bits_changed)
        self.tiff_compression_combo = QComboBox()
        self.tiff_compression_combo.addItem(t("wizard.step5.compression_none"), "none")
        self.tiff_compression_combo.addItem(t("wizard.step5.compression_lzw"), "lzw")
        self.tiff_compression_combo.currentIndexChanged.connect(self._on_tiff_compression_changed)
        self.tiff_colorspace_combo = QComboBox()
        self.tiff_colorspace_combo.addItem(t("wizard.step5.colorspace_srgb"), "srgb")
        self.tiff_colorspace_combo.addItem(t("wizard.step5.colorspace_gray"), "gray")
        self.tiff_colorspace_combo.currentIndexChanged.connect(self._on_tiff_colorspace_changed)
        tiff_form = QFormLayout()
        tiff_form.addRow(self.tiff_enabled_check)
        tiff_form.addRow(t("wizard.step5.bits"), self.tiff_bits_combo)
        tiff_form.addRow(t("wizard.step5.compression"), self.tiff_compression_combo)
        tiff_form.addRow(t("wizard.step5.colorspace"), self.tiff_colorspace_combo)
        tiff_group = QGroupBox(t("wizard.step5.tiff_group"))
        tiff_group.setLayout(tiff_form)

        self.jpeg_master_enabled_check = QCheckBox(t("wizard.step5.enabled"))
        self.jpeg_master_enabled_check.toggled.connect(self._on_jpeg_master_enabled_changed)
        self.jpeg_master_quality_spin = QSpinBox()
        self.jpeg_master_quality_spin.setRange(1, 100)
        self.jpeg_master_quality_spin.editingFinished.connect(self._on_jpeg_master_quality_changed)
        self.jpeg_master_long_edge_spin = QSpinBox()
        self.jpeg_master_long_edge_spin.setRange(0, 20000)
        self.jpeg_master_long_edge_spin.setSpecialValueText(t("wizard.step5.long_edge_full"))
        self.jpeg_master_long_edge_spin.editingFinished.connect(
            self._on_jpeg_master_long_edge_changed
        )
        jpeg_master_form = QFormLayout()
        jpeg_master_form.addRow(self.jpeg_master_enabled_check)
        jpeg_master_form.addRow(t("wizard.step5.quality"), self.jpeg_master_quality_spin)
        jpeg_master_form.addRow(t("wizard.step5.long_edge"), self.jpeg_master_long_edge_spin)
        jpeg_master_group = QGroupBox(t("wizard.step5.jpeg_master_group"))
        jpeg_master_group.setLayout(jpeg_master_form)

        self.jpeg_positive_enabled_check = QCheckBox(t("wizard.step5.enabled"))
        self.jpeg_positive_enabled_check.toggled.connect(self._on_jpeg_positive_enabled_changed)
        self.jpeg_positive_quality_spin = QSpinBox()
        self.jpeg_positive_quality_spin.setRange(1, 100)
        self.jpeg_positive_quality_spin.editingFinished.connect(
            self._on_jpeg_positive_quality_changed
        )
        self.jpeg_positive_long_edge_spin = QSpinBox()
        self.jpeg_positive_long_edge_spin.setRange(0, 20000)
        self.jpeg_positive_long_edge_spin.setSpecialValueText(t("wizard.step5.long_edge_full"))
        self.jpeg_positive_long_edge_spin.editingFinished.connect(
            self._on_jpeg_positive_long_edge_changed
        )
        self.jpeg_positive_mode_combo = QComboBox()
        self.jpeg_positive_mode_combo.addItem(t("wizard.step5.mode_simple"), "simple")
        self.jpeg_positive_mode_combo.addItem(t("wizard.step5.mode_auto"), "auto")
        self.jpeg_positive_mode_combo.addItem(t("wizard.step5.mode_manual"), "manual")
        self.jpeg_positive_mode_combo.currentIndexChanged.connect(
            self._on_jpeg_positive_mode_changed
        )
        self.jpeg_positive_flip_check = QCheckBox(t("wizard.step5.horizontal_flip"))
        self.jpeg_positive_flip_check.toggled.connect(self._on_jpeg_positive_flip_changed)

        jpeg_positive_form = QFormLayout()
        jpeg_positive_form.addRow(self.jpeg_positive_enabled_check)
        jpeg_positive_form.addRow(t("wizard.step5.quality"), self.jpeg_positive_quality_spin)
        jpeg_positive_form.addRow(t("wizard.step5.long_edge"), self.jpeg_positive_long_edge_spin)
        jpeg_positive_form.addRow(t("wizard.step5.mode"), self.jpeg_positive_mode_combo)
        jpeg_positive_form.addRow(self.jpeg_positive_flip_check)
        jpeg_positive_group = QGroupBox(t("wizard.step5.jpeg_positive_group"))
        jpeg_positive_group.setLayout(jpeg_positive_form)

        # Manual settings: never locked during capture, adjustable live
        # alongside the positive preview (P key).
        self.manual_exposure_spin = QDoubleSpinBox()
        self.manual_exposure_spin.setRange(-3.0, 3.0)
        self.manual_exposure_spin.setSingleStep(0.1)
        self.manual_exposure_spin.editingFinished.connect(self._on_manual_exposure_changed)
        self.manual_contrast_spin = QSpinBox()
        self.manual_contrast_spin.setRange(-100, 100)
        self.manual_contrast_spin.editingFinished.connect(self._on_manual_contrast_changed)
        self.manual_shadows_spin = QSpinBox()
        self.manual_shadows_spin.setRange(0, 100)
        self.manual_shadows_spin.editingFinished.connect(self._on_manual_shadows_changed)
        self.manual_highlights_spin = QSpinBox()
        self.manual_highlights_spin.setRange(0, 100)
        self.manual_highlights_spin.editingFinished.connect(self._on_manual_highlights_changed)
        manual_form = QFormLayout()
        manual_form.addRow(t("wizard.step5.exposure_ev"), self.manual_exposure_spin)
        manual_form.addRow(t("wizard.step5.contrast"), self.manual_contrast_spin)
        manual_form.addRow(t("wizard.step5.shadows"), self.manual_shadows_spin)
        manual_form.addRow(t("wizard.step5.highlights"), self.manual_highlights_spin)
        manual_group = QGroupBox(t("wizard.step5.manual_group"))
        manual_group.setLayout(manual_form)

        layout = QVBoxLayout()
        layout.addWidget(tiff_group)
        layout.addWidget(jpeg_master_group)
        layout.addWidget(jpeg_positive_group)
        layout.addWidget(manual_group)
        layout.addStretch(1)
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _refresh_exports(self) -> None:
        if self.campaign is None:
            return
        e = self.campaign.exports
        widgets = (
            self.tiff_enabled_check,
            self.tiff_bits_combo,
            self.tiff_compression_combo,
            self.tiff_colorspace_combo,
            self.jpeg_master_enabled_check,
            self.jpeg_master_quality_spin,
            self.jpeg_master_long_edge_spin,
            self.jpeg_positive_enabled_check,
            self.jpeg_positive_quality_spin,
            self.jpeg_positive_long_edge_spin,
            self.jpeg_positive_mode_combo,
            self.jpeg_positive_flip_check,
            self.manual_exposure_spin,
            self.manual_contrast_spin,
            self.manual_shadows_spin,
            self.manual_highlights_spin,
        )
        with ExitStack() as stack:
            for w in widgets:
                stack.enter_context(QSignalBlocker(w))
            self.tiff_enabled_check.setChecked(e.tiff.enabled)
            self.tiff_bits_combo.setCurrentText(str(e.tiff.bits))
            self.tiff_compression_combo.setCurrentIndex(
                self.tiff_compression_combo.findData(e.tiff.compression)
            )
            self.tiff_colorspace_combo.setCurrentIndex(
                self.tiff_colorspace_combo.findData(e.tiff.colorspace)
            )
            self.jpeg_master_enabled_check.setChecked(e.jpeg_master.enabled)
            self.jpeg_master_quality_spin.setValue(e.jpeg_master.quality)
            self.jpeg_master_long_edge_spin.setValue(e.jpeg_master.long_edge_px)
            self.jpeg_positive_enabled_check.setChecked(e.jpeg_positive.enabled)
            self.jpeg_positive_quality_spin.setValue(e.jpeg_positive.quality)
            self.jpeg_positive_long_edge_spin.setValue(e.jpeg_positive.long_edge_px)
            self.jpeg_positive_mode_combo.setCurrentIndex(
                self.jpeg_positive_mode_combo.findData(e.jpeg_positive.mode)
            )
            self.jpeg_positive_flip_check.setChecked(e.jpeg_positive.horizontal_flip)
            manual = e.jpeg_positive.manual_settings
            self.manual_exposure_spin.setValue(manual.exposure_ev)
            self.manual_contrast_spin.setValue(manual.contrast)
            self.manual_shadows_spin.setValue(manual.shadows)
            self.manual_highlights_spin.setValue(manual.highlights)

    def _on_tiff_enabled_changed(self, checked: bool) -> None:
        self._commit_export_field("tiff.enabled", "tiff", "enabled", bool(checked))

    def _on_tiff_bits_changed(self, _index: int) -> None:
        self._commit_export_field(
            "tiff.bits", "tiff", "bits", int(self.tiff_bits_combo.currentText())
        )

    def _on_tiff_compression_changed(self, _index: int) -> None:
        self._commit_export_field(
            "tiff.compression", "tiff", "compression", self.tiff_compression_combo.currentData()
        )

    def _on_tiff_colorspace_changed(self, _index: int) -> None:
        self._commit_export_field(
            "tiff.colorspace", "tiff", "colorspace", self.tiff_colorspace_combo.currentData()
        )

    def _on_jpeg_master_enabled_changed(self, checked: bool) -> None:
        self._commit_export_field("jpeg_master.enabled", "jpeg_master", "enabled", bool(checked))

    def _on_jpeg_master_quality_changed(self) -> None:
        self._commit_export_field(
            "jpeg_master.quality", "jpeg_master", "quality", self.jpeg_master_quality_spin.value()
        )

    def _on_jpeg_master_long_edge_changed(self) -> None:
        self._commit_export_field(
            "jpeg_master.long_edge_px",
            "jpeg_master",
            "long_edge_px",
            self.jpeg_master_long_edge_spin.value(),
        )

    def _on_jpeg_positive_enabled_changed(self, checked: bool) -> None:
        self._commit_export_field(
            "jpeg_positive.enabled", "jpeg_positive", "enabled", bool(checked)
        )

    def _on_jpeg_positive_quality_changed(self) -> None:
        self._commit_export_field(
            "jpeg_positive.quality",
            "jpeg_positive",
            "quality",
            self.jpeg_positive_quality_spin.value(),
        )

    def _on_jpeg_positive_long_edge_changed(self) -> None:
        self._commit_export_field(
            "jpeg_positive.long_edge_px",
            "jpeg_positive",
            "long_edge_px",
            self.jpeg_positive_long_edge_spin.value(),
        )

    def _on_jpeg_positive_mode_changed(self, _index: int) -> None:
        self._commit_export_field(
            "jpeg_positive.mode",
            "jpeg_positive",
            "mode",
            self.jpeg_positive_mode_combo.currentData(),
        )

    def _on_jpeg_positive_flip_changed(self, checked: bool) -> None:
        self._commit_export_field(
            "jpeg_positive.horizontal_flip", "jpeg_positive", "horizontal_flip", bool(checked)
        )

    def _commit_export_field(self, key: str, section: str, attr: str, after: Any) -> None:
        if self.campaign is None:
            return
        target = getattr(self.campaign.exports, section)
        before = getattr(target, attr)
        if after == before:
            return
        setattr(target, attr, after)
        self._commit(f"exports.{key}", before, after)

    def _on_manual_exposure_changed(self) -> None:
        self._commit_manual_setting("exposure_ev", self.manual_exposure_spin.value())

    def _on_manual_contrast_changed(self) -> None:
        self._commit_manual_setting("contrast", self.manual_contrast_spin.value())

    def _on_manual_shadows_changed(self) -> None:
        self._commit_manual_setting("shadows", self.manual_shadows_spin.value())

    def _on_manual_highlights_changed(self) -> None:
        self._commit_manual_setting("highlights", self.manual_highlights_spin.value())

    def _commit_manual_setting(self, attr: str, after: Any) -> None:
        """Manual settings: never locked, adjustable live."""
        if self.campaign is None:
            return
        target = self.campaign.exports.jpeg_positive.manual_settings
        before = getattr(target, attr)
        if after == before:
            return
        setattr(target, attr, after)
        self._commit(f"exports.jpeg_positive.manual_settings.{attr}", before, after)

    # --- Metadata (IPTC) --------------------------------------------------

    def _build_metadata_tab(self) -> QWidget:
        self.creator_edit = QLineEdit()
        self.creator_edit.editingFinished.connect(
            lambda: self._commit_iptc_field("creator", self.creator_edit.text().strip())
        )
        self.institution_iptc_edit = QLineEdit()
        self.institution_iptc_edit.editingFinished.connect(
            lambda: self._commit_iptc_field(
                "institution", self.institution_iptc_edit.text().strip()
            )
        )
        self.copyright_edit = QLineEdit()
        self.copyright_edit.editingFinished.connect(
            lambda: self._commit_iptc_field("copyright", self.copyright_edit.text().strip())
        )
        self.collection_edit = QLineEdit()
        self.collection_edit.editingFinished.connect(
            lambda: self._commit_iptc_field("collection", self.collection_edit.text().strip())
        )
        self.keywords_edit = QLineEdit()
        self.keywords_edit.editingFinished.connect(self._on_keywords_changed)

        form = QFormLayout()
        form.addRow(t("wizard.step6.creator"), self.creator_edit)
        form.addRow(t("wizard.step6.institution"), self.institution_iptc_edit)
        form.addRow(t("wizard.step6.copyright"), self.copyright_edit)
        form.addRow(t("wizard.step6.collection"), self.collection_edit)
        form.addRow(t("wizard.step6.keywords"), self.keywords_edit)
        widget = QWidget()
        widget.setLayout(form)
        return widget

    def _refresh_metadata(self) -> None:
        if self.campaign is None:
            return
        i = self.campaign.iptc
        widgets = (
            self.creator_edit,
            self.institution_iptc_edit,
            self.copyright_edit,
            self.collection_edit,
            self.keywords_edit,
        )
        with ExitStack() as stack:
            for w in widgets:
                stack.enter_context(QSignalBlocker(w))
            self.creator_edit.setText(i.creator)
            self.institution_iptc_edit.setText(i.institution)
            self.copyright_edit.setText(i.copyright)
            self.collection_edit.setText(i.collection)
            self.keywords_edit.setText(", ".join(i.keywords))

    def _commit_iptc_field(self, attr: str, after: str) -> None:
        if self.campaign is None:
            return
        before = getattr(self.campaign.iptc, attr)
        if after == before:
            return
        setattr(self.campaign.iptc, attr, after)
        self._commit(f"iptc.{attr}", before, after)

    def _on_keywords_changed(self) -> None:
        if self.campaign is None:
            return
        before = self.campaign.iptc.keywords
        after = [k.strip() for k in self.keywords_edit.text().split(",") if k.strip()]
        if after == before:
            return
        self.campaign.iptc.keywords = after
        self._commit("iptc.keywords", before, after)

    # --- CSV (06 §6) -------------------------------------------------------

    def _build_csv_tab(self) -> QWidget:
        self.csv_search_edit = QLineEdit()
        self.csv_search_edit.setPlaceholderText(t("project.csv_search_placeholder"))
        self.csv_search_edit.textChanged.connect(self._apply_csv_filter)

        self.csv_status_filter = QComboBox()
        self.csv_status_filter.addItem(t("project.csv_filter_all"), "")
        self.csv_status_filter.addItem(t("project.csv_filter_todo"), "todo")
        self.csv_status_filter.addItem(t("project.csv_filter_done"), "done")
        self.csv_status_filter.currentIndexChanged.connect(self._apply_csv_filter)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.csv_search_edit, 1)
        toolbar.addWidget(self.csv_status_filter)

        self.csv_table = CsvTableWidget()
        self.csv_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.csv_table.customContextMenuRequested.connect(self._show_csv_context_menu)

        layout = QVBoxLayout()
        layout.addLayout(toolbar)
        layout.addWidget(self.csv_table, 1)
        self._csv_tab_widget = QWidget()
        self._csv_tab_widget.setLayout(layout)
        return self._csv_tab_widget

    def _refresh_csv(self, *, filter_text: str = "", filter_status: str = "") -> None:
        if self.inventory is None:
            return
        rows = self.inventory.rows
        if filter_status:
            rows = [r for r in rows if r[STATUS_COLUMN] == filter_status]
        if filter_text:
            needle = filter_text.lower()
            rows = [r for r in rows if needle in r[self.inventory.name_column].lower()]
        cursor = self.inventory.current_index() if not filter_text and not filter_status else None
        self.csv_table.populate(
            fieldnames=self.inventory.fieldnames,
            name_column=self.inventory.name_column,
            rows=rows,
            cursor=cursor,
        )

    def refresh_csv_view(self) -> None:
        """Re-applies the current filter (e.g. after an external cursor change)."""
        self._apply_csv_filter()

    def _apply_csv_filter(self) -> None:
        self._refresh_csv(
            filter_text=self.csv_search_edit.text().strip(),
            filter_status=self.csv_status_filter.currentData() or "",
        )

    def focus_csv_search(self) -> None:
        """Ctrl+F; also used by Project ▸ CSV ▸ View."""
        self._tabs.setCurrentIndex(self._tabs.indexOf(self._csv_tab_widget))
        self.csv_search_edit.setFocus()
        self.csv_search_edit.selectAll()

    def reload_csv(self) -> None:
        """Project ▸ CSV ▸ Reload (03 §5.6, E-13)."""
        if self.campaign is None or self.paths is None or self.journal is None:
            return
        self.inventory = load_inventory(self.paths.inventory_csv, self.campaign.naming.csv_column)
        self.inventory.cursor = self.state.csv_cursor if self.state is not None else 0
        self.journal.log("CSV", "external_reload")
        self._refresh_csv()

    def export_csv(self, destination: Path) -> None:
        """Project ▸ CSV ▸ Export to… (03 §5.7)."""
        if self.paths is None or self.journal is None:
            return
        export_inventory(self.paths.inventory_csv, destination)
        self.journal.log("CSV", "exported", details={"to": str(destination)})

    def _show_csv_context_menu(self, pos: QPoint) -> None:
        row = self.csv_table.currentRow()
        if row < 0 or self.inventory is None:
            return
        name_item = self.csv_table.item(row, 1)
        if name_item is None:
            return
        row_data = self.inventory.row(name_item.text())
        if row_data is None or row_data[STATUS_COLUMN] == "done":
            return  # refused on a `done` row

        menu = QMenu(self)
        action = menu.addAction(t("project.csv_set_cursor_here"))
        action.triggered.connect(lambda: self.cursor_change_requested.emit(name_item.text()))
        menu.exec(self.csv_table.viewport().mapToGlobal(pos))

    # --- Log (06 §5) ------------------------------------------------------

    def _build_log_tab(self) -> QWidget:
        self.log_type_filter = QComboBox()
        self.log_type_filter.addItem(t("project.log_filter_all_types"), "")
        for event_type in (
            "PROJECT",
            "CSV",
            "CAPTURE",
            "FILE",
            "NAMING",
            "FRAMING",
            "EXPORT",
            "REJECT",
            "METADATA",
            "SYSTEM",
        ):
            self.log_type_filter.addItem(event_type, event_type)
        self.log_type_filter.currentIndexChanged.connect(self._refresh_log)

        self.log_level_filter = QComboBox()
        self.log_level_filter.addItem(t("project.log_filter_all_levels"), "")
        for level in ("info", "warn", "error"):
            self.log_level_filter.addItem(level, level)
        self.log_level_filter.currentIndexChanged.connect(self._refresh_log)

        open_logs_button = QPushButton(t("project.open_logs_folder"))
        open_logs_button.clicked.connect(self._open_logs_folder)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.log_type_filter)
        toolbar.addWidget(self.log_level_filter)
        toolbar.addStretch(1)
        toolbar.addWidget(open_logs_button)

        self.log_table = LogTableWidget()

        layout = QVBoxLayout()
        layout.addLayout(toolbar)
        layout.addWidget(self.log_table, 1)
        self._log_tab_widget = QWidget()
        self._log_tab_widget.setLayout(layout)
        return self._log_tab_widget

    def _refresh_log(self, *_args: object) -> None:
        if self.paths is None:
            return
        self.log_table.load_today(
            self.paths.logs_dir,
            type_filter=self.log_type_filter.currentData() or "",
            level_filter=self.log_level_filter.currentData() or "",
        )

    def show_log_tab(self) -> None:
        """Project ▸ Today's log (06 §12)."""
        self._tabs.setCurrentIndex(self._tabs.indexOf(self._log_tab_widget))

    def _open_logs_folder(self) -> None:
        if self.paths is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.logs_dir)))

    def open_campaign_folder(self) -> None:
        """Project ▸ Open campaign folder (06 §12)."""
        self._open_campaign_folder()
