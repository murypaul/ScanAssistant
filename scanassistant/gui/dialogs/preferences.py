"""Preferences dialog: everything in `config.json` (global, not per-campaign).

Every field applies immediately on change and is persisted right away,
same convention as Project ▸ Campaign settings — no separate "Save"
action. Disabled from the menu during an active capture session, same as
Help ▸ Check for updates.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from scanassistant.app_context import AppContext
from scanassistant.camera.backend import is_available as is_camera_available
from scanassistant.config import load_config, save_config
from scanassistant.gui.shortcuts import (
    CONTEXTS,
    DEFAULT_SHORTCUTS,
    conflicting_action,
    is_allowed_key,
    merge_with_defaults,
)
from scanassistant.gui.update_worker import CameraDependencyInstallWorker
from scanassistant.i18n import t
from scanassistant.metadata.writer import is_available as is_exiftool_available
from scanassistant.updater import UpdateApplyResult

_ACTION_LABEL_KEYS: dict[str, dict[str, str]] = {
    "capture": {
        "finalize": "preferences.shortcut_finalize",
        "reject": "preferences.shortcut_reject",
        "rotate": "preferences.shortcut_rotate",
        "recompute_frame": "preferences.shortcut_recompute_frame",
        "toggle_guides": "preferences.shortcut_toggle_guides",
        "positive_preview": "preferences.shortcut_positive_preview",
        "master_preview": "preferences.shortcut_master_preview",
        "cycle_preview": "preferences.shortcut_cycle_preview",
        "go_to_name": "preferences.shortcut_go_to_name",
        "trigger_capture": "preferences.shortcut_trigger_capture",
        "pause_resume": "preferences.shortcut_pause_resume",
        "toggle_live_view": "preferences.shortcut_toggle_live_view",
        "toggle_live_view_panel": "preferences.shortcut_toggle_live_view_panel",
        "pick_white_balance": "preferences.shortcut_pick_white_balance",
        "stop_capture": "preferences.shortcut_stop_capture",
    },
    "name_conflict": {
        "option_1": "preferences.shortcut_option_1",
        "option_2": "preferences.shortcut_option_2",
        "option_3": "preferences.shortcut_option_3",
    },
    "global": {
        "new_campaign": "preferences.shortcut_new_campaign",
        "open_campaign": "preferences.shortcut_open_campaign",
        "quit": "preferences.shortcut_quit",
        "search_csv": "preferences.shortcut_search_csv",
        "start_capture": "preferences.shortcut_start_capture",
        "shortcuts_help": "preferences.shortcut_shortcuts_help",
        "fullscreen": "preferences.shortcut_fullscreen",
    },
}

_CONTEXT_LABEL_KEYS = {
    "capture": "preferences.shortcuts_context_capture",
    "name_conflict": "preferences.shortcuts_context_name_conflict",
    "global": "preferences.shortcuts_context_global",
}


class _KeyCaptureButton(QPushButton):
    """Click, then press a key: emits the captured `QKeySequence` string.

    Clicking elsewhere (focus loss) cancels without emitting — there's no
    dedicated cancel key, since Escape must itself stay assignable.
    """

    key_captured = Signal(str)

    def __init__(self, key_string: str, parent: QWidget | None = None) -> None:
        super().__init__(key_string, parent)
        self._capturing = False
        self._display_text = key_string
        self.clicked.connect(self._begin_capture)

    def set_key_string(self, key_string: str) -> None:
        self._display_text = key_string
        self.setText(key_string)

    def _begin_capture(self) -> None:
        self._capturing = True
        self.setText(t("preferences.shortcuts_press_key"))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._capturing:
            super().keyPressEvent(event)
            return
        self._capturing = False
        key_string = QKeySequence(event.keyCombination()).toString()
        self.setText(self._display_text)
        event.accept()
        self.key_captured.emit(key_string)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        if self._capturing:
            self._capturing = False
            self.setText(self._display_text)
        super().focusOutEvent(event)


class PreferencesDialog(QDialog):
    def __init__(
        self,
        context: AppContext,
        *,
        app_dir: Path,
        check_updates: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self._app_dir = app_dir
        self._check_updates = check_updates
        self._camera_install_worker: CameraDependencyInstallWorker | None = None
        self._shortcuts = merge_with_defaults(context.config.shortcuts)
        self.setWindowTitle(t("preferences.title"))
        self.setMinimumSize(560, 560)

        tabs = QTabWidget()
        self._add_scrollable_tab(tabs, self._build_general_tab(), t("preferences.tab_general"))
        self._add_scrollable_tab(
            tabs, self._build_processing_tab(), t("preferences.tab_processing")
        )
        self._add_scrollable_tab(
            tabs, self._build_thresholds_tab(), t("preferences.tab_thresholds")
        )
        self._add_scrollable_tab(tabs, self._build_updates_tab(), t("preferences.tab_updates"))
        self._add_scrollable_tab(tabs, self._build_camera_tab(), t("preferences.tab_camera"))
        self._add_scrollable_tab(tabs, self._build_shortcuts_tab(), t("preferences.tab_shortcuts"))

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

    @staticmethod
    def _add_scrollable_tab(tabs: QTabWidget, content: QWidget, label: str) -> None:
        """Every tab's content can outgrow the dialog (Shortcuts especially,
        four contexts' worth of rows) — a `QTabWidget` doesn't scroll its
        pages on its own, so each one gets wrapped here rather than getting
        clipped or forcing the whole dialog to grow past the screen."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        tabs.addTab(scroll, label)

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

    # --- Camera ----------------------------------------------------------------

    def _build_camera_tab(self) -> QWidget:
        self.camera_enabled_check = QCheckBox(t("preferences.camera_enabled"))
        self.camera_enabled_check.setToolTip(t("preferences.camera_enabled_tooltip"))
        self.camera_enabled_check.setChecked(self.context.config.camera.enabled)
        self.camera_enabled_check.toggled.connect(self._on_camera_enabled_changed)

        restart_note = QLabel(t("preferences.camera_enabled_restart_note"))
        restart_note.setWordWrap(True)

        self.camera_rotate_180_check = QCheckBox(t("preferences.camera_rotate_180"))
        self.camera_rotate_180_check.setToolTip(t("preferences.camera_rotate_180_tooltip"))
        self.camera_rotate_180_check.setChecked(self.context.config.camera.live_view_rotate_180)
        self.camera_rotate_180_check.toggled.connect(self._on_camera_rotate_180_changed)

        layout = QVBoxLayout()
        layout.addWidget(self.camera_enabled_check)
        layout.addWidget(restart_note)
        layout.addWidget(self.camera_rotate_180_check)
        layout.addStretch(1)
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _on_camera_rotate_180_changed(self, checked: bool) -> None:
        self.context.config.camera.live_view_rotate_180 = bool(checked)
        self._save()

    def _on_camera_enabled_changed(self, checked: bool) -> None:
        if not checked or is_camera_available():
            self.context.config.camera.enabled = bool(checked)
            self._save()
            return

        # Turning tethered capture on for the first time in this venv:
        # `gphoto2` is an opt-in extra (pyproject.toml `camera`), never
        # installed by default. Offer to install it right now rather than
        # persisting `enabled` and leaving the operator to hit
        # `ModuleNotFoundError` the next time Capture opens.
        self.camera_enabled_check.blockSignals(True)
        self.camera_enabled_check.setChecked(False)
        self.camera_enabled_check.blockSignals(False)

        answer = QMessageBox.question(
            self,
            t("preferences.camera_install_title"),
            t("preferences.camera_install_question"),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.camera_enabled_check.setEnabled(False)
        worker = CameraDependencyInstallWorker(self._app_dir, sys.executable)
        worker.finished_install.connect(self._on_camera_install_finished)
        self._camera_install_worker = worker
        worker.start()

    def _on_camera_install_finished(self, result: UpdateApplyResult) -> None:
        self._camera_install_worker = None
        self.camera_enabled_check.setEnabled(True)
        if result.success:
            self.camera_enabled_check.blockSignals(True)
            self.camera_enabled_check.setChecked(True)
            self.camera_enabled_check.blockSignals(False)
            self.context.config.camera.enabled = True
            self._save()
            QMessageBox.information(
                self,
                t("preferences.camera_install_title"),
                t("preferences.camera_install_success"),
            )
        else:
            QMessageBox.warning(
                self,
                t("preferences.camera_install_title"),
                t("preferences.camera_install_failed", error=result.error),
            )

    # --- Shortcuts -----------------------------------------------------------

    def _build_shortcuts_tab(self) -> QWidget:
        self._shortcut_buttons: dict[tuple[str, str], _KeyCaptureButton] = {}
        self._shortcuts_status = QLabel()
        self._shortcuts_status.setWordWrap(True)

        layout = QVBoxLayout()
        for context in CONTEXTS:
            section = QLabel(f"<b>{t(_CONTEXT_LABEL_KEYS[context])}</b>")
            layout.addWidget(section)
            form = QFormLayout()
            for action in DEFAULT_SHORTCUTS[context]:
                row = QHBoxLayout()
                button = _KeyCaptureButton(self._shortcuts[context][action])
                button.key_captured.connect(
                    lambda key_string, c=context, a=action: self._on_shortcut_captured(
                        c, a, key_string
                    )
                )
                reset_button = QPushButton(t("preferences.shortcuts_reset"))
                reset_button.clicked.connect(
                    lambda _checked=False, c=context, a=action: self._on_reset_shortcut(c, a)
                )
                row.addWidget(button, 1)
                row.addWidget(reset_button)
                form.addRow(t(_ACTION_LABEL_KEYS[context][action]), row)
                self._shortcut_buttons[(context, action)] = button
            layout.addLayout(form)

        reset_all_button = QPushButton(t("preferences.shortcuts_reset_all"))
        reset_all_button.clicked.connect(self._on_reset_all_shortcuts)

        layout.addWidget(self._shortcuts_status)
        layout.addWidget(reset_all_button)
        layout.addStretch(1)

        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _on_shortcut_captured(self, context: str, action: str, key_string: str) -> None:
        if not is_allowed_key(key_string, context=context):
            self._shortcuts_status.setText(
                t("preferences.shortcuts_invalid_key", shortcut=key_string)
            )
            return
        conflict = conflicting_action(self._shortcuts[context], key_string, exclude_action=action)
        if conflict is not None:
            self._shortcuts_status.setText(
                t(
                    "preferences.shortcuts_conflict",
                    shortcut=key_string,
                    action=t(_ACTION_LABEL_KEYS[context][conflict]),
                )
            )
            return
        self._shortcuts_status.setText("")
        self._shortcuts[context][action] = key_string
        self._shortcut_buttons[(context, action)].set_key_string(key_string)
        self._save_shortcuts()

    def _on_reset_shortcut(self, context: str, action: str) -> None:
        default = DEFAULT_SHORTCUTS[context][action]
        self._shortcuts[context][action] = default
        self._shortcut_buttons[(context, action)].set_key_string(default)
        self._save_shortcuts()

    def _on_reset_all_shortcuts(self) -> None:
        self._shortcuts = merge_with_defaults({})
        for (context, action), button in self._shortcut_buttons.items():
            button.set_key_string(self._shortcuts[context][action])
        self._shortcuts_status.setText("")
        self._save_shortcuts()

    def _save_shortcuts(self) -> None:
        self.context.config.shortcuts = self._shortcuts
        self._save()

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
        self._refresh_shortcuts()

    def _refresh_shortcuts(self) -> None:
        self._shortcuts = merge_with_defaults(self.context.config.shortcuts)
        for (context, action), button in self._shortcut_buttons.items():
            button.set_key_string(self._shortcuts[context][action])
        self._shortcuts_status.setText("")

    def _refresh_thresholds(self) -> None:
        c = self.context.config
        self.disk_warn_spin.setValue(c.thresholds.disk_warn_gb)
        self.disk_critical_spin.setValue(c.thresholds.disk_critical_gb)
        self.export_queue_warn_spin.setValue(c.thresholds.export_queue_warn)
