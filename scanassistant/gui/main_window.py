"""Main window.

Only one top-level screen visible at a time (home or project); classic
menu bar. Menu items not applicable to the current mode are shown
disabled rather than omitted.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from scanassistant import __version__
from scanassistant.app_context import AppContext
from scanassistant.config import save_config
from scanassistant.core.export_runner import MasterExportRunner
from scanassistant.core.fs import RealFileSystem
from scanassistant.core.session import CaptureSession
from scanassistant.gui.errors import format_business_error
from scanassistant.gui.screens.capture import CaptureScreen
from scanassistant.gui.screens.home import HomeScreen
from scanassistant.gui.screens.project import ProjectScreen
from scanassistant.gui.screens.statistics import StatisticsScreen
from scanassistant.gui.theme import apply_theme
from scanassistant.gui.widgets.export_queue_panel import ExportQueuePanel
from scanassistant.gui.widgets.history_panel import HistoryPanel
from scanassistant.gui.widgets.pin_checkbox import make_pin_checkbox
from scanassistant.gui.widgets.positive_settings_panel import PositiveSettingsPanel
from scanassistant.gui.wizard.new_campaign import NewCampaignWizard
from scanassistant.i18n import t
from scanassistant.imaging.raw import RawpyDecoder
from scanassistant.journal.journal import Journal
from scanassistant.metadata.writer import ExifToolMetadataWriter
from scanassistant.metadata.writer import is_available as is_exiftool_available
from scanassistant.project.campaign import Campaign, open_campaign, save_campaign
from scanassistant.project.errors import InvalidCampaignError, ScanAssistantError
from scanassistant.project.inventory import Inventory
from scanassistant.project.lock import ProjectLock, acquire_lock
from scanassistant.watcher.monitor import FolderMonitor

_SHORTCUTS_TEXT = """\
Ctrl+N   New campaign (outside capture)
Ctrl+O   Open a campaign (outside capture)
Ctrl+Q   Quit
Ctrl+F   Search in the CSV viewer
F5       Start capture (preparation)
F11      Full screen
F1       This help

Capture mode (from M4):
Enter    Finalize the current image        R   Reject the current image
V        Rotate 90°                          G   Go to name
Left/Right  Previous/next name              C   Recompute frame
M        Edit frame                         P   Positive preview
T        Master preview                     Space   Pause / Resume
Escape   Stop capture
"""


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self._lock: ProjectLock | None = None
        self._lock_was_stale = False
        self._journal: Journal | None = None
        self._shortcuts_window: QWidget | None = None

        self.setWindowTitle(t("home.title"))
        self.setMinimumSize(1280, 720)

        self.home_screen = HomeScreen()
        self.home_screen.new_campaign_requested.connect(self._on_new_campaign)
        self.home_screen.open_campaign_requested.connect(self._on_open_campaign)
        self.home_screen.recent_campaign_chosen.connect(lambda p: self._open_project(Path(p)))
        self.home_screen.set_recent_projects(context.config.general.recent_projects)

        self.project_screen = ProjectScreen()
        self.project_screen.cursor_change_requested.connect(self._on_cursor_change_requested)

        self.capture_screen = CaptureScreen()
        self.capture_screen.stopped.connect(self._on_capture_stopped)
        self.capture_screen.queue_changed.connect(self._refresh_export_queue_panel)
        self.capture_screen.queue_changed.connect(self._refresh_history_panel)

        self.statistics_screen = StatisticsScreen()

        self._stack = QStackedWidget()
        self._stack.addWidget(self.home_screen)
        self._stack.addWidget(self.project_screen)
        self._stack.addWidget(self.capture_screen)
        self.setCentralWidget(self._stack)

        self.export_queue_panel = ExportQueuePanel()
        self.export_queue_dock = QDockWidget(t("export_queue.title"), self)
        self.export_queue_dock.setObjectName("exportQueueDock")
        self.export_queue_dock.setWidget(self.export_queue_panel)
        self.export_queue_dock.setVisible(False)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.export_queue_dock)

        self.history_panel = HistoryPanel()
        self.history_panel.image_activated.connect(self._on_history_image_activated)
        self.history_dock = QDockWidget(t("history.title"), self)
        self.history_dock.setObjectName("historyDock")
        self.history_dock.setWidget(self.history_panel)
        self.history_dock.setVisible(False)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.history_dock)

        self.positive_settings_panel = PositiveSettingsPanel()
        self.positive_settings_panel.setting_changed.connect(self._on_positive_setting_changed)
        self.positive_settings_dock = QDockWidget(t("positive_settings.title"), self)
        self.positive_settings_dock.setObjectName("positiveSettingsDock")
        self.positive_settings_dock.setWidget(self.positive_settings_panel)
        self.positive_settings_dock.setVisible(False)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.positive_settings_dock)

        self._build_menus()
        self._show_home()

    # --- menus -----------------------------------------------------------------

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        self.file_menu = menu_bar.addMenu(t("menu.file"))
        self._add_action(self.file_menu, t("menu.file_new"), "Ctrl+N", self._on_new_campaign)
        self._add_action(self.file_menu, t("menu.file_open"), "Ctrl+O", self._on_open_campaign)
        self.recent_menu = self.file_menu.addMenu(t("menu.file_recent"))
        self._rebuild_recent_menu()
        self.file_menu.addSeparator()
        self._add_action(self.file_menu, t("menu.file_quit"), "Ctrl+Q", self.close)

        self.project_menu = menu_bar.addMenu(t("menu.project"))
        self.action_campaign_settings = self._add_action(
            self.project_menu, t("menu.project_settings"), None, self._on_campaign_settings
        )
        csv_menu = self.project_menu.addMenu(t("menu.project_csv"))
        self._add_action(
            csv_menu, t("menu.project_csv_view"), None, self.project_screen.focus_csv_search
        )
        self._add_action(
            csv_menu, t("menu.project_csv_reload"), None, self.project_screen.reload_csv
        )
        self._add_action(csv_menu, t("menu.project_csv_export"), None, self._on_csv_export)
        self.action_statistics = self._add_action(
            self.project_menu, t("menu.project_statistics"), None, self._on_open_statistics
        )
        self._add_action(
            self.project_menu,
            t("menu.project_open_folder"),
            None,
            self.project_screen.open_campaign_folder,
        )
        self._add_action(
            self.project_menu, t("menu.project_today_log"), None, self.project_screen.show_log_tab
        )
        self.project_menu.setEnabled(False)

        # Only "Start capture" carries a shortcut here (preparation context):
        # the others reuse CAPTURE-context gestures (Esc/Space/Return/R/G),
        # already handled in a single place, `CaptureScreen.keyPressEvent` —
        # giving them a menu shortcut too would double-trigger them.
        self.capture_menu = menu_bar.addMenu(t("menu.capture"))
        self.action_start_capture = self._add_action(
            self.capture_menu, t("menu.capture_start"), "F5", self._on_start_capture
        )
        self.action_start_capture.setEnabled(False)
        self.action_stop_capture = self._add_action(
            self.capture_menu, t("menu.capture_stop"), None, None
        )
        self.action_pause_resume = self._add_action(
            self.capture_menu, t("menu.capture_pause_resume"), None, None
        )
        self.action_finalize = self._add_action(
            self.capture_menu, t("menu.capture_finalize"), None, None
        )
        self.action_reject = self._add_action(
            self.capture_menu, t("menu.capture_reject"), None, None
        )
        self.action_rename = self._add_action(
            self.capture_menu, t("menu.capture_rename"), None, None
        )
        self.action_rename.setEnabled(False)  # Capture ▸ Rename current image: not implemented yet
        self.action_go_to_name = self._add_action(
            self.capture_menu, t("menu.capture_go_to_name"), None, None
        )
        for action in (
            self.action_stop_capture,
            self.action_pause_resume,
            self.action_finalize,
            self.action_reject,
            self.action_go_to_name,
        ):
            action.setEnabled(False)

        # As with the Capture menu above, no shortcut is attached here:
        # C/M/V/P/T are already handled by `CaptureScreen.keyPressEvent`, a
        # `QAction.setShortcut` would double-trigger on every keypress.
        self.processing_menu = menu_bar.addMenu(t("menu.processing"))
        self.action_recompute_frame = self._add_action(
            self.processing_menu, t("menu.processing_recompute_frame"), None, None
        )
        self.action_edit_frame = self._add_action(
            self.processing_menu, t("menu.processing_edit_frame"), None, None
        )
        self.action_rotate_image = self._add_action(
            self.processing_menu, t("menu.processing_rotate"), None, None
        )
        self.action_positive_preview = self._add_action(
            self.processing_menu, t("menu.processing_positive_preview"), None, None
        )
        self.action_master_preview = self._add_action(
            self.processing_menu, t("menu.processing_master_preview"), None, None
        )
        self.action_regenerate = self._add_action(
            self.processing_menu, t("menu.processing_regenerate"), None, None
        )
        for action in (
            self.action_recompute_frame,
            self.action_edit_frame,
            self.action_rotate_image,
            self.action_positive_preview,
            self.action_master_preview,
            self.action_regenerate,
        ):
            action.setEnabled(False)

        self.metadata_menu = menu_bar.addMenu(t("menu.metadata"))
        self._add_action(
            self.metadata_menu, t("menu.metadata_campaign_iptc"), None, self._on_campaign_settings
        )
        self._add_action(
            self.metadata_menu,
            t("menu.metadata_check_exiftool"),
            None,
            self._on_check_exiftool,
        )

        self.view_menu = menu_bar.addMenu(t("menu.view"))
        self.action_fullscreen = self._add_action(
            self.view_menu, t("menu.view_fullscreen"), "F11", self._toggle_fullscreen
        )
        self.action_fullscreen.setCheckable(True)

        brightness_menu = self.view_menu.addMenu(t("menu.view_brightness"))
        brightness_group = QActionGroup(self)
        brightness_group.setExclusive(True)
        for value, label in (
            ("normal", t("menu.view_brightness_normal")),
            ("dimmed", t("menu.view_brightness_dimmed")),
            ("minimal", t("menu.view_brightness_minimal")),
        ):
            action = brightness_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.context.config.ui.brightness == value)
            action.triggered.connect(lambda _checked=False, v=value: self._set_brightness(v))
            brightness_group.addAction(action)

        self.action_export_queue = self._add_action(
            self.view_menu, t("menu.view_export_queue"), None, self._toggle_export_queue_panel
        )
        self.action_export_queue.setCheckable(True)
        self.export_queue_dock.visibilityChanged.connect(self.action_export_queue.setChecked)

        self.action_history = self._add_action(
            self.view_menu, t("menu.view_history"), None, self._toggle_history_panel
        )
        self.action_history.setCheckable(True)
        self.history_dock.visibilityChanged.connect(self.action_history.setChecked)

        self.action_positive_settings = self._add_action(
            self.view_menu,
            t("menu.view_positive_settings"),
            None,
            self._toggle_positive_settings_panel,
        )
        self.action_positive_settings.setCheckable(True)
        self.positive_settings_dock.visibilityChanged.connect(
            self.action_positive_settings.setChecked
        )

        self.help_menu = menu_bar.addMenu(t("menu.help"))
        self._add_action(self.help_menu, t("menu.help_shortcuts"), "F1", self._show_shortcuts)
        self._add_action(self.help_menu, t("menu.help_about"), None, self._show_about)

    def _add_action(self, menu: QMenu, label: str, shortcut: str | None, slot: object) -> QAction:
        action = QAction(label, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if slot is not None:
            action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        recent = self.context.config.general.recent_projects
        if not recent:
            empty = self.recent_menu.addAction(t("home.recent_empty"))
            empty.setEnabled(False)
            return
        for path_str in recent:
            action = self.recent_menu.addAction(path_str)
            action.triggered.connect(lambda _checked=False, p=path_str: self._open_project(Path(p)))

    # --- screen navigation ---------------------------------------------------

    def _show_home(self) -> None:
        self._stack.setCurrentWidget(self.home_screen)
        self.project_menu.setEnabled(False)
        self.action_start_capture.setEnabled(False)

    def _show_project(self) -> None:
        self._stack.setCurrentWidget(self.project_screen)
        self.project_menu.setEnabled(True)
        self.action_start_capture.setEnabled(True)

    def _show_capture(self) -> None:
        self._stack.setCurrentWidget(self.capture_screen)
        self.project_menu.setEnabled(False)
        self.capture_screen.setFocus()

    # --- campaign creation / opening --------------------------------------------

    def _on_new_campaign(self) -> None:
        wizard = NewCampaignWizard(self)
        if wizard.exec() == QDialog.DialogCode.Accepted and wizard.result_campaign:
            self._open_project(wizard.result_campaign.paths.root)

    def _on_open_campaign(self) -> None:
        path = QFileDialog.getExistingDirectory(self, t("home.open_campaign"))
        if path:
            self._open_project(Path(path))

    def _open_project(self, root: Path) -> None:
        try:
            opened = open_campaign(root)
        except ScanAssistantError as exc:
            QMessageBox.critical(self, t("home.open_failed_title"), format_business_error(exc))
            return
        except OSError as exc:
            QMessageBox.critical(self, t("home.open_failed_title"), str(exc))
            return
        try:
            lock = acquire_lock(opened.paths.lock_file)
        except ScanAssistantError as exc:
            QMessageBox.critical(self, t("home.open_failed_title"), format_business_error(exc))
            return

        self._release_lock()
        self._lock = lock
        self._lock_was_stale = lock.was_stale  # consumed once by `_on_start_capture`
        journal = Journal(opened.paths.logs_dir)
        journal.log("PROJECT", "opened", details={"name": opened.campaign.name})
        self._journal = journal

        self.project_screen.load(
            campaign=opened.campaign,
            state=opened.state,
            inventory=opened.inventory,
            paths=opened.paths,
            journal=journal,
        )
        self._remember_recent(str(root))
        self._show_project()

    def _remember_recent(self, path: str) -> None:
        self.context.config.general = self.context.config.general.with_recent_project(path)
        save_config(self.context.config)
        self.home_screen.set_recent_projects(self.context.config.general.recent_projects)
        self._rebuild_recent_menu()

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    # --- Project menu actions ---------------------------------------------------

    def _on_campaign_settings(self) -> None:
        self.project_screen.show_settings_tab()

    def _on_csv_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, t("menu.project_csv_export"), "", "CSV (*.csv)")
        if path:
            self.project_screen.export_csv(Path(path))

    # --- statistics and completeness --------------------------------------------

    def _on_open_statistics(self) -> None:
        # Reuses the current capture session if there is one (up-to-date
        # counters, including `error_images`); otherwise builds an offline
        # one, never started (`initial_scan`/`pump` never called) — Statistics
        # is also accessible outside of capture.
        session = self.capture_screen.session or self._build_offline_session()
        if session is None:
            QMessageBox.information(self, t("statistics.title"), t("statistics.unavailable"))
            return
        self.statistics_screen.load(session)
        self.statistics_screen.show()
        self.statistics_screen.raise_()
        self.statistics_screen.activateWindow()

    def _build_offline_session(self) -> CaptureSession | None:
        campaign = self.project_screen.campaign
        state = self.project_screen.state
        inventory = self.project_screen.inventory
        paths = self.project_screen.paths
        journal = self.project_screen.journal
        if (
            campaign is None
            or state is None
            or inventory is None
            or paths is None
            or journal is None
        ):
            return None
        monitor = FolderMonitor(
            Path(campaign.capture.watched_folder or paths.root),
            campaign.capture.extensions,
            watch_mode="polling",
            stabilization_delay_s=campaign.capture.stabilization_delay_s,
            stabilization_timeout_s=campaign.capture.stabilization_timeout_s,
        )
        export_runner = MasterExportRunner(
            decoder=RawpyDecoder(),
            campaign=campaign,
            paths=paths,
            metadata_writer=ExifToolMetadataWriter(executable=self.context.config.paths.exiftool),
            journal=journal,
        )
        return CaptureSession(
            paths=paths,
            campaign=campaign,
            inventory=inventory,
            state=state,
            journal=journal,
            fs=RealFileSystem(),
            monitor=monitor,
            export_runner=export_runner,
        )

    # --- "Export queue" panel ---------------------------------------------------

    def _toggle_export_queue_panel(self) -> None:
        self.export_queue_dock.setVisible(not self.export_queue_dock.isVisible())
        self._refresh_export_queue_panel()

    def _refresh_export_queue_panel(self) -> None:
        session = self.capture_screen.session
        tasks = session.export_queue.pending_tasks() if session is not None else []
        self.export_queue_panel.refresh(tasks)

    # --- "Session history" panel ------------------------------------------------

    def _toggle_history_panel(self) -> None:
        self.history_dock.setVisible(not self.history_dock.isVisible())
        self._refresh_history_panel()

    def _refresh_history_panel(self) -> None:
        session = self.capture_screen.session
        if session is None:
            self.history_panel.clear_history()
            return
        self.history_panel.refresh(
            session.session_history(), session.paths, session.campaign.exports.jpeg_positive.suffix
        )

    def _on_history_image_activated(self, name: str) -> None:
        self.capture_screen.reopen_image_for_correction(name)

    # --- "Positive settings" panel ----------------------------------------------

    def _toggle_positive_settings_panel(self) -> None:
        self.positive_settings_dock.setVisible(not self.positive_settings_dock.isVisible())

    def _on_positive_setting_changed(self, key: str, before: object, after: object) -> None:
        session = self.capture_screen.session
        if session is None:
            return
        try:
            save_campaign(session.campaign, session.paths.campaign_json)
        except InvalidCampaignError as exc:
            QMessageBox.warning(
                self, t("project.invalid_setting_title"), format_business_error(exc)
            )
            return
        session.journal.log(
            "PROJECT", "setting_changed", details={"key": key, "before": before, "after": after}
        )

    # --- metadata ----------------------------------------------------------

    def _on_check_exiftool(self) -> None:
        available = is_exiftool_available(self.context.config.paths.exiftool)
        message = (
            t("metadata.exiftool_available") if available else t("metadata.exiftool_unavailable")
        )
        QMessageBox.information(self, t("menu.metadata_check_exiftool"), message)

    def _on_cursor_change_requested(self, name: str) -> None:
        inventory = self.project_screen.inventory
        state = self.project_screen.state
        paths = self.project_screen.paths
        journal = self.project_screen.journal
        if inventory is None or state is None or paths is None or journal is None:
            return
        before = inventory.cursor
        try:
            inventory.go_to_name(name)
        except ValueError as exc:
            QMessageBox.warning(self, t("project.invalid_setting_title"), str(exc))
            return
        state.csv_cursor = inventory.cursor
        journal.log(
            "CSV",
            "cursor",
            details={"before": before, "after": inventory.cursor, "cause": "manual"},
        )
        self.project_screen.refresh_csv_view()

    # --- capture mode --------------------------------------------------------

    _CAPTURE_ACTION_SLOTS = (
        "action_stop_capture",
        "action_pause_resume",
        "action_finalize",
        "action_reject",
        "action_go_to_name",
    )

    # Regenerate stays disabled permanently: it would duplicate Statistics ▸
    # Regenerate selection. C/M/V/P/T are already functional via keyboard
    # and wired here to the Processing menu.
    _PROCESSING_ACTION_SLOTS = (
        "action_recompute_frame",
        "action_edit_frame",
        "action_rotate_image",
        "action_positive_preview",
        "action_master_preview",
    )

    def _on_start_capture(self) -> None:
        campaign = self.project_screen.campaign
        state = self.project_screen.state
        inventory = self.project_screen.inventory
        paths = self.project_screen.paths
        journal = self.project_screen.journal
        if (
            campaign is None
            or state is None
            or inventory is None
            or paths is None
            or journal is None
        ):
            return

        error = self._check_capture_entry_conditions(campaign, inventory)
        if error:
            QMessageBox.warning(self, t("capture.cannot_start_title"), error)
            return

        self.capture_screen.start(
            campaign=campaign,
            state=state,
            inventory=inventory,
            paths=paths,
            journal=journal,
            fs=RealFileSystem(),
            exiftool_executable=self.context.config.paths.exiftool,
            was_stale=self._lock_was_stale,
        )
        self._lock_was_stale = False  # consumed: no double recovery
        self._wire_capture_actions()
        self.positive_settings_panel.load(campaign.exports.jpeg_positive)
        self._show_capture()

    def _check_capture_entry_conditions(
        self, campaign: Campaign, inventory: Inventory
    ) -> str | None:
        """Simplified subset of the conditions required to enter capture mode.

        Free disk space and write access to project folders (beyond the
        watched folder) are handled by the full monitoring loop once
        capture has started; only conditions checkable without extra
        dependencies are verified here.
        """
        if inventory.is_exhausted():
            return t("capture.error_csv_exhausted")

        watched_text = campaign.capture.watched_folder
        watched_folder = Path(watched_text) if watched_text else None
        if watched_folder is None or not watched_folder.is_dir():
            return t("capture.error_watched_folder_missing")

        try:
            RealFileSystem().touch_and_remove(watched_folder / ".scanassistant_probe")
        except OSError as exc:
            return t("capture.error_watched_folder_inaccessible", error=str(exc))

        return None

    def _wire_capture_actions(self) -> None:
        self.action_start_capture.setEnabled(False)
        for action_name, slot in zip(
            self._CAPTURE_ACTION_SLOTS,
            (
                self.capture_screen.stop_capture,
                self.capture_screen.toggle_pause,
                self.capture_screen.finalize_current,
                self.capture_screen.reject_current_image,
                self.capture_screen.open_go_to_name,
            ),
            strict=True,
        ):
            action: QAction = getattr(self, action_name)
            action.setEnabled(True)
            action.triggered.connect(slot)

        for action_name, slot in zip(
            self._PROCESSING_ACTION_SLOTS,
            (
                self.capture_screen.recompute_frame,
                self.capture_screen.enter_edit_mode,
                self.capture_screen.rotate_image_action,
                self.capture_screen.toggle_positive_preview,
                self.capture_screen.toggle_master_preview,
            ),
            strict=True,
        ):
            action = getattr(self, action_name)
            action.setEnabled(True)
            action.triggered.connect(slot)

    def _unwire_capture_actions(self) -> None:
        for action_name, slot in zip(
            self._CAPTURE_ACTION_SLOTS,
            (
                self.capture_screen.stop_capture,
                self.capture_screen.toggle_pause,
                self.capture_screen.finalize_current,
                self.capture_screen.reject_current_image,
                self.capture_screen.open_go_to_name,
            ),
            strict=True,
        ):
            action: QAction = getattr(self, action_name)
            with contextlib.suppress(TypeError, RuntimeError):
                action.triggered.disconnect(slot)
            action.setEnabled(False)

        for action_name, slot in zip(
            self._PROCESSING_ACTION_SLOTS,
            (
                self.capture_screen.recompute_frame,
                self.capture_screen.enter_edit_mode,
                self.capture_screen.rotate_image_action,
                self.capture_screen.toggle_positive_preview,
                self.capture_screen.toggle_master_preview,
            ),
            strict=True,
        ):
            action = getattr(self, action_name)
            with contextlib.suppress(TypeError, RuntimeError):
                action.triggered.disconnect(slot)
            action.setEnabled(False)

    def _on_capture_stopped(self) -> None:
        self._unwire_capture_actions()
        self._refresh_history_panel()
        self.positive_settings_panel.clear_panel()
        self.project_screen.refresh()
        self._show_project()

    # --- full screen -------------------------------------------------------

    def _toggle_fullscreen(self, checked: bool) -> None:
        if checked:
            self.showFullScreen()
            self.menuBar().setVisible(False)
        else:
            self.showNormal()
            self.menuBar().setVisible(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.isFullScreen() and event.key() == Qt.Key.Key_Alt:
            self.menuBar().setVisible(True)
        super().keyPressEvent(event)

    # --- interface brightness ---------------------------------------------------

    def _set_brightness(self, value: str) -> None:
        """Persists and applies `ui.brightness` (dims text/surfaces)."""
        self.context.config.ui.brightness = value
        save_config(self.context.config)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, value)

    # --- help ----------------------------------------------------------------

    def _show_shortcuts(self) -> None:
        # No parent: a `QWidget(self)` would be embedded as a *child* of the
        # main window (no title bar, not closable/movable) rather than
        # becoming its own top-level window — same fix as `StatisticsScreen`.
        if self._shortcuts_window is None:
            window = QWidget()
            window.setWindowTitle(t("menu.help_shortcuts"))
            window.resize(520, 360)
            text_edit = QTextEdit(window)
            text_edit.setReadOnly(True)
            text_edit.setPlainText(_SHORTCUTS_TEXT)
            layout = QVBoxLayout(window)
            layout.addWidget(make_pin_checkbox(window))
            layout.addWidget(text_edit)
            self._shortcuts_window = window
        self._shortcuts_window.show()
        self._shortcuts_window.raise_()
        self._shortcuts_window.activateWindow()

    def _show_about(self) -> None:
        QMessageBox.about(self, t("menu.help_about"), t("app.version_line", version=__version__))

    # --- clean shutdown ----------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.capture_screen.session is not None:
            self.capture_screen.stop()  # finalizes the current image
        self._release_lock()
        super().closeEvent(event)
