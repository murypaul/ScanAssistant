"""New-campaign wizard, seven steps.

Everything stays editable afterward in the project screen: this wizard
only builds a `Campaign` in memory and calls
`project.campaign.create_campaign()` on the last step. Full validation
(`Campaign.validate()`) isn't duplicated here — only minimal checks drive
whether "Next" is enabled (non-empty name, folders chosen, valid CSV);
any deeper error (invalid characters, numeric bounds) surfaces at
creation time and sends the operator back to the wizard rather than
closing the window.
"""

from __future__ import annotations

from pathlib import Path

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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from scanassistant.gui.widgets.csv_table import CsvTableWidget
from scanassistant.i18n import t
from scanassistant.project.campaign import Campaign, CreatedCampaign, create_campaign
from scanassistant.project.errors import InvalidCampaignError, InvalidCsvError
from scanassistant.project.inventory import ImportedInventory, import_csv


def _set_role(widget: QWidget, role: str) -> None:
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class IdentityPage(QWizardPage):
    """Step 1: identity."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle(t("wizard.step1.title"))
        self.setSubTitle(t("wizard.step1.subtitle"))

        self.name_edit = QLineEdit()
        self.name_edit.setMinimumWidth(420)
        self.name_edit.textChanged.connect(lambda _: self.completeChanged.emit())
        self.description_edit = QPlainTextEdit()
        self.description_edit.setFixedHeight(60)
        self.operator_edit = QLineEdit()
        self.institution_edit = QLineEdit()

        form = QFormLayout(self)
        form.addRow(t("wizard.step1.name"), self.name_edit)
        form.addRow(t("wizard.step1.description"), self.description_edit)
        form.addRow(t("wizard.step1.operator"), self.operator_edit)
        form.addRow(t("wizard.step1.institution"), self.institution_edit)

    def isComplete(self) -> bool:
        return bool(self.name_edit.text().strip())


class FoldersPage(QWizardPage):
    """Step 2: folders."""

    def __init__(self, identity_page: IdentityPage) -> None:
        super().__init__()
        self._identity_page = identity_page
        self.setTitle(t("wizard.step2.title"))
        self.setSubTitle(t("wizard.step2.subtitle"))

        self.parent_dir_edit = QLineEdit()
        self.parent_dir_edit.textChanged.connect(self._on_changed)
        parent_browse = QPushButton(t("wizard.browse"))
        parent_browse.clicked.connect(self._browse_parent)
        parent_row = QHBoxLayout()
        parent_row.addWidget(self.parent_dir_edit, 1)
        parent_row.addWidget(parent_browse)

        self.root_preview_label = QLabel()
        self.root_preview_label.setProperty("role", "secondary")

        self.watched_folder_edit = QLineEdit()
        self.watched_folder_edit.textChanged.connect(self._on_changed)
        watched_browse = QPushButton(t("wizard.browse"))
        watched_browse.clicked.connect(self._browse_watched)
        watched_row = QHBoxLayout()
        watched_row.addWidget(self.watched_folder_edit, 1)
        watched_row.addWidget(watched_browse)

        watched_help = QLabel(t("wizard.step2.watched_folder_help"))
        watched_help.setWordWrap(True)
        watched_help.setProperty("role", "secondary")

        form = QFormLayout()
        form.addRow(t("wizard.step2.location"), parent_row)
        form.addRow("", self.root_preview_label)
        form.addRow(t("wizard.step2.watched_folder"), watched_row)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(watched_help)

    def initializePage(self) -> None:
        self._on_changed()

    def _browse_parent(self) -> None:
        path = QFileDialog.getExistingDirectory(self, t("wizard.step2.location"))
        if path:
            self.parent_dir_edit.setText(path)

    def _browse_watched(self) -> None:
        path = QFileDialog.getExistingDirectory(self, t("wizard.step2.watched_folder"))
        if path:
            self.watched_folder_edit.setText(path)

    def _on_changed(self) -> None:
        parent = self.parent_dir_edit.text().strip()
        name = self._identity_page.name_edit.text().strip()
        if parent and name:
            self.root_preview_label.setText(
                t("wizard.step2.campaign_folder_preview", root=str(Path(parent) / name))
            )
        else:
            self.root_preview_label.setText("")
        self.completeChanged.emit()

    @property
    def root(self) -> Path:
        return (
            Path(self.parent_dir_edit.text().strip()) / self._identity_page.name_edit.text().strip()
        )

    def isComplete(self) -> bool:
        return bool(self.parent_dir_edit.text().strip()) and bool(
            self.watched_folder_edit.text().strip()
        )


class CsvPage(QWizardPage):
    """Step 3: CSV."""

    PREVIEW_LIMIT = 20

    def __init__(self) -> None:
        super().__init__()
        self.setTitle(t("wizard.step3.title"))
        self.setSubTitle(t("wizard.step3.subtitle"))

        self.csv_path_edit = QLineEdit()
        self.csv_path_edit.setReadOnly(True)
        browse = QPushButton(t("wizard.browse"))
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.csv_path_edit, 1)
        path_row.addWidget(browse)

        self.name_column_edit = QLineEdit("filename")
        self.name_column_edit.editingFinished.connect(self._revalidate)

        self.report_label = QLabel()
        self.report_label.setWordWrap(True)

        self.preview_table = CsvTableWidget()

        form = QFormLayout()
        form.addRow(t("wizard.step3.csv_path"), path_row)
        form.addRow(t("wizard.step3.name_column"), self.name_column_edit)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.report_label)
        layout.addWidget(QLabel(t("wizard.step3.preview_label")))
        layout.addWidget(self.preview_table, 1)

        self._imported: ImportedInventory | None = None

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, t("wizard.step3.csv_browse"), "", "CSV (*.csv)")
        if path:
            self.csv_path_edit.setText(path)
            self._revalidate()

    def _revalidate(self) -> None:
        self._imported = None
        self.preview_table.setRowCount(0)
        path_text = self.csv_path_edit.text().strip()
        if not path_text:
            self.report_label.setText("")
            self.completeChanged.emit()
            return

        try:
            imported = import_csv(
                Path(path_text), self.name_column_edit.text().strip() or "filename"
            )
        except InvalidCsvError as exc:
            problems = exc.details.get("problems", [])
            assert isinstance(problems, list)
            lines = [t("wizard.step3.validation_errors", count=len(problems))]
            lines += [f"• {p}" for p in problems]
            self.report_label.setText("\n".join(lines))
            _set_role(self.report_label, "critical")
            self.completeChanged.emit()
            return

        self._imported = imported
        suffix = (
            t("wizard.step3.validation_warnings", count=len(imported.warnings))
            if imported.warnings
            else ""
        )
        self.report_label.setText(
            t("wizard.step3.validation_ok", rows=imported.rows_imported) + suffix
        )
        _set_role(self.report_label, "ok")
        self.preview_table.populate(
            fieldnames=imported.inventory.fieldnames,
            name_column=imported.inventory.name_column,
            rows=imported.inventory.rows,
            limit=self.PREVIEW_LIMIT,
        )
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._imported is not None

    @property
    def csv_path(self) -> Path:
        return Path(self.csv_path_edit.text().strip())

    @property
    def name_column(self) -> str:
        assert self._imported is not None
        return self._imported.inventory.name_column


class FramingPage(QWizardPage):
    """Step 4: capture and framing."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle(t("wizard.step4.title"))
        self.setSubTitle(t("wizard.step4.subtitle"))

        self.enabled_check = QCheckBox(t("wizard.step4.framing_enabled"))
        self.enabled_check.setChecked(True)

        self.orientation_combo = QComboBox()
        self.orientation_combo.addItem(t("common.horizontal"), "horizontal")
        self.orientation_combo.addItem(t("common.vertical"), "vertical")

        self.size_mode_combo = QComboBox()
        self.size_mode_combo.addItem(t("wizard.step4.size_mode_native"), "native")
        self.size_mode_combo.addItem(t("wizard.step4.size_mode_fixed"), "fixed")
        self.size_mode_combo.currentIndexChanged.connect(self._update_dimensions_enabled)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(512, 20000)
        self.width_spin.setValue(6016)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(512, 20000)
        self.height_spin.setValue(4016)

        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0, 20)
        self.margin_spin.setSuffix(" %")
        self.margin_spin.setValue(2.0)

        form = QFormLayout(self)
        form.addRow(self.enabled_check)
        form.addRow(t("wizard.step4.default_orientation"), self.orientation_combo)
        form.addRow(t("wizard.step4.size_mode"), self.size_mode_combo)
        form.addRow(t("wizard.step4.final_width"), self.width_spin)
        form.addRow(t("wizard.step4.final_height"), self.height_spin)
        form.addRow(t("wizard.step4.margin_pct"), self.margin_spin)

        self._update_dimensions_enabled()

    def _update_dimensions_enabled(self) -> None:
        fixed = self.size_mode_combo.currentData() == "fixed"
        self.width_spin.setEnabled(fixed)
        self.height_spin.setEnabled(fixed)


class ExportsPage(QWizardPage):
    """Step 5: exports."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle(t("wizard.step5.title"))
        self.setSubTitle(t("wizard.step5.subtitle"))

        self.tiff_enabled = QCheckBox(t("wizard.step5.enabled"))
        self.tiff_enabled.setChecked(True)
        self.tiff_bits = QComboBox()
        self.tiff_bits.addItems(["8", "16"])
        self.tiff_bits.setCurrentText("16")
        self.tiff_compression = QComboBox()
        self.tiff_compression.addItem(t("wizard.step5.compression_none"), "none")
        self.tiff_compression.addItem(t("wizard.step5.compression_lzw"), "lzw")
        self.tiff_compression.setCurrentIndex(1)
        self.tiff_colorspace = QComboBox()
        self.tiff_colorspace.addItem(t("wizard.step5.colorspace_srgb"), "srgb")
        self.tiff_colorspace.addItem(t("wizard.step5.colorspace_gray"), "gray")

        tiff_form = QFormLayout()
        tiff_form.addRow(self.tiff_enabled)
        tiff_form.addRow(t("wizard.step5.bits"), self.tiff_bits)
        tiff_form.addRow(t("wizard.step5.compression"), self.tiff_compression)
        tiff_form.addRow(t("wizard.step5.colorspace"), self.tiff_colorspace)
        tiff_group = QGroupBox(t("wizard.step5.tiff_group"))
        tiff_group.setLayout(tiff_form)

        self.jpeg_master_enabled = QCheckBox(t("wizard.step5.enabled"))
        self.jpeg_master_enabled.setChecked(True)
        self.jpeg_master_quality = QSpinBox()
        self.jpeg_master_quality.setRange(1, 100)
        self.jpeg_master_quality.setValue(92)
        self.jpeg_master_long_edge = QSpinBox()
        self.jpeg_master_long_edge.setRange(0, 20000)
        self.jpeg_master_long_edge.setValue(0)
        self.jpeg_master_long_edge.setSpecialValueText(t("wizard.step5.long_edge_full"))

        jpeg_master_form = QFormLayout()
        jpeg_master_form.addRow(self.jpeg_master_enabled)
        jpeg_master_form.addRow(t("wizard.step5.quality"), self.jpeg_master_quality)
        jpeg_master_form.addRow(t("wizard.step5.long_edge"), self.jpeg_master_long_edge)
        jpeg_master_group = QGroupBox(t("wizard.step5.jpeg_master_group"))
        jpeg_master_group.setLayout(jpeg_master_form)

        self.jpeg_positive_enabled = QCheckBox(t("wizard.step5.enabled"))
        self.jpeg_positive_enabled.setChecked(True)
        self.jpeg_positive_quality = QSpinBox()
        self.jpeg_positive_quality.setRange(1, 100)
        self.jpeg_positive_quality.setValue(90)
        self.jpeg_positive_long_edge = QSpinBox()
        self.jpeg_positive_long_edge.setRange(0, 20000)
        self.jpeg_positive_long_edge.setValue(3000)
        self.jpeg_positive_long_edge.setSpecialValueText(t("wizard.step5.long_edge_full"))
        self.jpeg_positive_mode = QComboBox()
        self.jpeg_positive_mode.addItem(t("wizard.step5.mode_simple"), "simple")
        self.jpeg_positive_mode.addItem(t("wizard.step5.mode_auto"), "auto")
        self.jpeg_positive_mode.addItem(t("wizard.step5.mode_manual"), "manual")
        self.jpeg_positive_mode.setCurrentIndex(1)
        self.jpeg_positive_flip = QCheckBox(t("wizard.step5.horizontal_flip"))
        self.jpeg_positive_flip.setChecked(True)

        jpeg_positive_form = QFormLayout()
        jpeg_positive_form.addRow(self.jpeg_positive_enabled)
        jpeg_positive_form.addRow(t("wizard.step5.quality"), self.jpeg_positive_quality)
        jpeg_positive_form.addRow(t("wizard.step5.long_edge"), self.jpeg_positive_long_edge)
        jpeg_positive_form.addRow(t("wizard.step5.mode"), self.jpeg_positive_mode)
        jpeg_positive_form.addRow(self.jpeg_positive_flip)
        jpeg_positive_group = QGroupBox(t("wizard.step5.jpeg_positive_group"))
        jpeg_positive_group.setLayout(jpeg_positive_form)

        layout = QVBoxLayout(self)
        layout.addWidget(tiff_group)
        layout.addWidget(jpeg_master_group)
        layout.addWidget(jpeg_positive_group)


class MetadataPage(QWizardPage):
    """Step 6: IPTC metadata."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle(t("wizard.step6.title"))
        self.setSubTitle(t("wizard.step6.subtitle"))

        self.creator_edit = QLineEdit()
        self.institution_edit = QLineEdit()
        self.copyright_edit = QLineEdit()
        self.collection_edit = QLineEdit()
        self.keywords_edit = QLineEdit()
        self.keywords_edit.setPlaceholderText(t("wizard.step6.keywords_placeholder"))

        form = QFormLayout(self)
        form.addRow(t("wizard.step6.creator"), self.creator_edit)
        form.addRow(t("wizard.step6.institution"), self.institution_edit)
        form.addRow(t("wizard.step6.copyright"), self.copyright_edit)
        form.addRow(t("wizard.step6.collection"), self.collection_edit)
        form.addRow(t("wizard.step6.keywords"), self.keywords_edit)

    @property
    def keywords(self) -> list[str]:
        return [k.strip() for k in self.keywords_edit.text().split(",") if k.strip()]


class SummaryPage(QWizardPage):
    """Step 7: summary → creation."""

    def __init__(self, wizard: NewCampaignWizard) -> None:
        super().__init__()
        self._wizard = wizard
        self.setTitle(t("wizard.step7.title"))
        self.setSubTitle(t("wizard.step7.subtitle"))
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary_label)

    def initializePage(self) -> None:
        w = self._wizard
        lines = [
            f"{t('wizard.step1.name')}: {w.identity_page.name_edit.text()}",
            f"{t('wizard.step2.location')}: {w.folders_page.root}",
            f"{t('wizard.step2.watched_folder')}: {w.folders_page.watched_folder_edit.text()}",
            f"{t('wizard.step3.csv_path')}: {w.csv_page.csv_path}",
        ]
        self.summary_label.setText("\n".join(lines))

    def validatePage(self) -> bool:
        return self._wizard.create_campaign_now()


class NewCampaignWizard(QWizard):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("wizard.title"))
        self.setOption(QWizard.WizardOption.NoBackButtonOnLastPage, False)
        self.setButtonText(QWizard.WizardButton.FinishButton, t("wizard.step7.create_button"))
        self.setMinimumSize(720, 560)

        self.identity_page = IdentityPage()
        self.folders_page = FoldersPage(self.identity_page)
        self.csv_page = CsvPage()
        self.framing_page = FramingPage()
        self.exports_page = ExportsPage()
        self.metadata_page = MetadataPage()

        self.addPage(self.identity_page)
        self.addPage(self.folders_page)
        self.addPage(self.csv_page)
        self.addPage(self.framing_page)
        self.addPage(self.exports_page)
        self.addPage(self.metadata_page)
        self.addPage(SummaryPage(self))

        self.result_campaign: CreatedCampaign | None = None

    def create_campaign_now(self) -> bool:
        campaign = self._build_campaign()
        try:
            self.result_campaign = create_campaign(
                self.folders_page.root, campaign, self.csv_page.csv_path
            )
        except (InvalidCampaignError, InvalidCsvError) as exc:
            QMessageBox.critical(self, t("wizard.creation_failed_title"), str(exc))
            return False
        except OSError as exc:
            QMessageBox.critical(self, t("wizard.creation_failed_title"), str(exc))
            return False
        return True

    def _build_campaign(self) -> Campaign:
        campaign = Campaign(name=self.identity_page.name_edit.text().strip())
        campaign.description = self.identity_page.description_edit.toPlainText().strip()
        campaign.operator = self.identity_page.operator_edit.text().strip()
        campaign.institution = self.identity_page.institution_edit.text().strip()

        campaign.capture.watched_folder = self.folders_page.watched_folder_edit.text().strip()

        campaign.naming.csv_column = self.csv_page.name_column

        campaign.framing.enabled = self.framing_page.enabled_check.isChecked()
        campaign.framing.default_orientation = self.framing_page.orientation_combo.currentData()
        campaign.framing.size_mode = self.framing_page.size_mode_combo.currentData()
        campaign.framing.final_dimensions_px = [
            self.framing_page.width_spin.value(),
            self.framing_page.height_spin.value(),
        ]
        campaign.framing.margin_pct = self.framing_page.margin_spin.value()

        campaign.exports.tiff.enabled = self.exports_page.tiff_enabled.isChecked()
        campaign.exports.tiff.bits = int(self.exports_page.tiff_bits.currentText())
        campaign.exports.tiff.compression = self.exports_page.tiff_compression.currentData()
        campaign.exports.tiff.colorspace = self.exports_page.tiff_colorspace.currentData()

        campaign.exports.jpeg_master.enabled = self.exports_page.jpeg_master_enabled.isChecked()
        campaign.exports.jpeg_master.quality = self.exports_page.jpeg_master_quality.value()
        campaign.exports.jpeg_master.long_edge_px = self.exports_page.jpeg_master_long_edge.value()

        campaign.exports.jpeg_positive.enabled = self.exports_page.jpeg_positive_enabled.isChecked()
        campaign.exports.jpeg_positive.quality = self.exports_page.jpeg_positive_quality.value()
        campaign.exports.jpeg_positive.long_edge_px = (
            self.exports_page.jpeg_positive_long_edge.value()
        )
        campaign.exports.jpeg_positive.mode = self.exports_page.jpeg_positive_mode.currentData()
        campaign.exports.jpeg_positive.horizontal_flip = (
            self.exports_page.jpeg_positive_flip.isChecked()
        )

        campaign.iptc.creator = self.metadata_page.creator_edit.text().strip()
        campaign.iptc.institution = self.metadata_page.institution_edit.text().strip()
        campaign.iptc.copyright = self.metadata_page.copyright_edit.text().strip()
        campaign.iptc.collection = self.metadata_page.collection_edit.text().strip()
        campaign.iptc.keywords = self.metadata_page.keywords

        return campaign
