"""Preferences dialog: everything in `config.json` (global, not per-campaign).

Every field applies immediately on change and is persisted right away,
same convention as Project ▸ Campaign settings — no separate "Save"
action. Disabled from the menu during an active capture session, same as
Help ▸ Check for updates.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from scanassistant.app_context import AppContext
from scanassistant.config import load_config, save_config
from scanassistant.i18n import t
from scanassistant.metadata.writer import is_available as is_exiftool_available


class PreferencesDialog(QDialog):
    def __init__(
        self,
        context: AppContext,
        *,
        check_updates: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self._check_updates = check_updates
        self.setWindowTitle(t("preferences.title"))
        self.setMinimumSize(480, 420)

        tabs = QTabWidget()
        tabs.addTab(self._build_general_tab(), t("preferences.tab_general"))
        tabs.addTab(self._build_processing_tab(), t("preferences.tab_processing"))
        tabs.addTab(self._build_thresholds_tab(), t("preferences.tab_thresholds"))
        tabs.addTab(self._build_updates_tab(), t("preferences.tab_updates"))

        export_button = QPushButton(t("preferences.export_settings"))
        export_button.clicked.connect(self._on_export_settings)
        import_button = QPushButton(t("preferences.import_settings"))
        import_button.clicked.connect(self._on_import_settings)
        io_row = QHBoxLayout()
        io_row.addWidget(export_button)
        io_row.addWidget(import_button)
        io_row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addLayout(io_row)
        layout.addWidget(buttons)

        self._refresh_all()

    # --- General -------------------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        self.reopen_last_check = QCheckBox(t("preferences.reopen_last"))
        self.reopen_last_check.toggled.connect(self._on_reopen_last_changed)

        layout = QVBoxLayout()
        layout.addWidget(self.reopen_last_check)
        layout.addStretch(1)
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _on_reopen_last_changed(self, checked: bool) -> None:
        self.context.config.general.reopen_last = bool(checked)
        self._save()

    # --- Processing ------------------------------------------------------------

    def _build_processing_tab(self) -> QWidget:
        self.exiftool_edit = QLineEdit()
        self.exiftool_edit.setToolTip(t("preferences.exiftool_tooltip"))
        self.exiftool_edit.editingFinished.connect(self._on_exiftool_changed)
        browse_button = QPushButton(t("wizard.browse"))
        browse_button.clicked.connect(self._on_browse_exiftool)
        test_button = QPushButton(t("preferences.test"))
        test_button.clicked.connect(self._on_test_exiftool)
        exiftool_row = QHBoxLayout()
        exiftool_row.addWidget(self.exiftool_edit, 1)
        exiftool_row.addWidget(browse_button)
        exiftool_row.addWidget(test_button)

        self.drain_on_exit_check = QCheckBox(t("preferences.drain_on_exit"))
        self.drain_on_exit_check.setToolTip(t("preferences.drain_on_exit_tooltip"))
        self.drain_on_exit_check.toggled.connect(self._on_drain_on_exit_changed)

        self.max_name_length_spin = QSpinBox()
        self.max_name_length_spin.setRange(10, 300)
        self.max_name_length_spin.setToolTip(t("preferences.max_name_length_tooltip"))
        self.max_name_length_spin.editingFinished.connect(self._on_max_name_length_changed)

        form = QFormLayout()
        form.addRow(t("preferences.exiftool"), exiftool_row)
        form.addRow(self.drain_on_exit_check)
        form.addRow(t("preferences.max_name_length"), self.max_name_length_spin)

        widget = QWidget()
        widget.setLayout(form)
        return widget

    def _on_browse_exiftool(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, t("preferences.exiftool"))
        if path:
            self.exiftool_edit.setText(path)
            self._on_exiftool_changed()

    def _on_exiftool_changed(self) -> None:
        self.context.config.paths.exiftool = self.exiftool_edit.text().strip()
        self._save()

    def _on_test_exiftool(self) -> None:
        available = is_exiftool_available(self.context.config.paths.exiftool)
        message = (
            t("metadata.exiftool_available") if available else t("metadata.exiftool_unavailable")
        )
        QMessageBox.information(self, t("preferences.test"), message)

    def _on_drain_on_exit_changed(self, checked: bool) -> None:
        self.context.config.processing.drain_on_exit = bool(checked)
        self._save()

    def _on_max_name_length_changed(self) -> None:
        self.context.config.csv.max_name_length = self.max_name_length_spin.value()
        self._save()

    # --- Thresholds --------------------------------------------------------

    def _build_thresholds_tab(self) -> QWidget:
        self.disk_warn_spin = QSpinBox()
        self.disk_warn_spin.setRange(1, 500)
        self.disk_warn_spin.setSuffix(" GB")
        self.disk_warn_spin.setToolTip(t("preferences.disk_warn_tooltip"))
        self.disk_warn_spin.editingFinished.connect(self._on_disk_thresholds_changed)

        self.disk_critical_spin = QSpinBox()
        self.disk_critical_spin.setRange(1, 100)
        self.disk_critical_spin.setSuffix(" GB")
        self.disk_critical_spin.setToolTip(t("preferences.disk_critical_tooltip"))
        self.disk_critical_spin.editingFinished.connect(self._on_disk_thresholds_changed)

        self.export_queue_warn_spin = QSpinBox()
        self.export_queue_warn_spin.setRange(5, 500)
        self.export_queue_warn_spin.setToolTip(t("preferences.export_queue_warn_tooltip"))
        self.export_queue_warn_spin.editingFinished.connect(self._on_export_queue_warn_changed)

        form = QFormLayout()
        form.addRow(t("preferences.disk_warn"), self.disk_warn_spin)
        form.addRow(t("preferences.disk_critical"), self.disk_critical_spin)
        form.addRow(t("preferences.export_queue_warn"), self.export_queue_warn_spin)

        widget = QWidget()
        widget.setLayout(form)
        return widget

    def _on_disk_thresholds_changed(self) -> None:
        before_warn = self.context.config.thresholds.disk_warn_gb
        before_critical = self.context.config.thresholds.disk_critical_gb
        self.context.config.thresholds.disk_warn_gb = self.disk_warn_spin.value()
        self.context.config.thresholds.disk_critical_gb = self.disk_critical_spin.value()
        if not self._save():
            self.context.config.thresholds.disk_warn_gb = before_warn
            self.context.config.thresholds.disk_critical_gb = before_critical
            self._refresh_thresholds()

    def _on_export_queue_warn_changed(self) -> None:
        self.context.config.thresholds.export_queue_warn = self.export_queue_warn_spin.value()
        self._save()

    # --- Updates -------------------------------------------------------------

    def _build_updates_tab(self) -> QWidget:
        self.check_enabled_check = QCheckBox(t("preferences.updates_check_enabled"))
        self.check_enabled_check.setToolTip(t("preferences.updates_check_enabled_tooltip"))
        self.check_enabled_check.toggled.connect(self._on_check_enabled_changed)

        check_now_button = QPushButton(t("menu.help_check_updates"))
        check_now_button.setEnabled(self._check_updates is not None)
        check_now_button.clicked.connect(self._on_check_now)

        layout = QVBoxLayout()
        layout.addWidget(self.check_enabled_check)
        layout.addWidget(check_now_button)
        layout.addStretch(1)
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _on_check_enabled_changed(self, checked: bool) -> None:
        self.context.config.updates.check_enabled = bool(checked)
        self._save()

    def _on_check_now(self) -> None:
        if self._check_updates is not None:
            self._check_updates()

    # --- export / import of the whole file --------------------------------

    def _on_export_settings(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, t("preferences.export_settings"), "config.json", "JSON (*.json)"
        )
        if not path:
            return
        Path(path).write_text(
            json.dumps(asdict(self.context.config), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _on_import_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, t("preferences.import_settings"), "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            imported = load_config(Path(path))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, t("preferences.import_settings"), str(exc))
            return
        self.context.config = imported
        save_config(self.context.config)
        self._refresh_all()
        QMessageBox.information(
            self, t("preferences.import_settings"), t("preferences.restart_notice")
        )

    # --- persistence / refresh ----------------------------------------------

    def _save(self) -> bool:
        try:
            save_config(self.context.config)
        except ValueError as exc:
            QMessageBox.warning(self, t("preferences.title"), str(exc))
            return False
        return True

    def _refresh_all(self) -> None:
        c = self.context.config
        self.reopen_last_check.setChecked(c.general.reopen_last)
        self.exiftool_edit.setText(c.paths.exiftool)
        self.drain_on_exit_check.setChecked(c.processing.drain_on_exit)
        self.max_name_length_spin.setValue(c.csv.max_name_length)
        self._refresh_thresholds()
        self.check_enabled_check.setChecked(c.updates.check_enabled)

    def _refresh_thresholds(self) -> None:
        c = self.context.config
        self.disk_warn_spin.setValue(c.thresholds.disk_warn_gb)
        self.disk_critical_spin.setValue(c.thresholds.disk_critical_gb)
        self.export_queue_warn_spin.setValue(c.thresholds.export_queue_warn)
