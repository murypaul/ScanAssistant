"""Main window.

Only one top-level screen visible at a time (home or project); classic
menu bar. Menu items not applicable to the current mode are shown
disabled rather than omitted.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
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
from scanassistant.core.queue import ThreadedExportExecutor
from scanassistant.core.session import CaptureSession
from scanassistant.gui.dialogs.preferences import PreferencesDialog
from scanassistant.gui.errors import format_business_error
from scanassistant.gui.screens.capture import CaptureScreen
from scanassistant.gui.screens.home import HomeScreen
from scanassistant.gui.screens.positive_review import PositiveReviewScreen
from scanassistant.gui.screens.project import ProjectScreen
from scanassistant.gui.screens.statistics import StatisticsScreen
from scanassistant.gui.shortcuts import (
    CAPTURE,
    GLOBAL,
    NAME_CONFLICT,
    merge_with_defaults,
)
from scanassistant.gui.theme import apply_theme
from scanassistant.gui.update_worker import UpdateApplyWorker, UpdateCheckWorker
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
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.lock import ProjectLock, acquire_lock
from scanassistant.updater import UpdateApplyResult, UpdateCheckResult
from scanassistant.watcher.monitor import FolderMonitor


def _build_shortcuts_text(shortcuts: dict[str, dict[str, str]]) -> str:
    g, c, nc = (
        shortcuts[GLOBAL],
        shortcuts[CAPTURE],
        shortcuts[NAME_CONFLICT],
    )
    lines = [
        "Global:",
        f"  {g['new_campaign']}  New campaign (outside capture)",
        f"  {g['open_campaign']}  Open a campaign (outside capture)",
        f"  {g['quit']}  Quit",
        f"  {g['search_csv']}  Search in the CSV viewer",
        f"  {g['start_capture']}  Start capture (preparation)",
        f"  {g['fullscreen']}  Full screen",
        f"  {g['shortcuts_help']}  This help",
        "",
        "Capture mode:",
        f"  {c['finalize']}  Finalize the current image",
        f"  {c['reject']}  Reject the current image",
        f"  {c['rotate']}  Rotate 90° (Shift+{c['rotate']}: the other way)",
        f"  {c['go_to_name']}  Go to name",
        f"  {c['recompute_frame']}  Recompute frame",
        f"  {c['positive_preview']}  Positive preview",
        f"  {c['master_preview']}  Master preview",
        f"  {c['cycle_preview']}  Cycle preview (negative / positive / master,"
        f" Shift+{c['cycle_preview']}: the other way)",
        f"  {c['trigger_capture']}  Trigger the camera remotely (tethered camera only)",
        f"  {c['pause_resume']}  Pause / Resume",
        f"  {c['toggle_live_view']}  Toggle live view (tethered camera only)",
        f"  {c['toggle_live_view_panel']}  Show/hide the live view panel"
        " (tethered camera only — also View menu)",
        f"  {c['pick_white_balance']}  Pick white balance from a neutral point in the preview"
        " (applies to the rest of the session)",
        f"  {c['stop_capture']}  Stop capture",
        "",
        "Crop, always available in capture mode:",
        "  Arrows  Move the frame (Shift: x10)",
        "  +/-  Resize (Shift: larger step)",
        "  Ctrl+Arrows  Rotate (Shift: x10)",
        f"  {c['toggle_guides']}  Toggle rule-of-thirds guides",
        "  Drag the frame's border or interior with the mouse to resize or move it",
        "",
        "Name conflict:",
        f"  {nc['option_1']} / {nc['option_2']} / {nc['option_3']}  Pick an option",
        "  Tab  Move between fields",
    ]
    return "\n".join(lines)


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self._shortcuts = merge_with_defaults(context.config.shortcuts)
        self._lock: ProjectLock | None = None
        self._lock_was_stale = False
        self._journal: Journal | None = None
        self._shortcuts_window: QWidget | None = None
        self._shortcuts_text_edit: QTextEdit | None = None
        self._update_check_worker: UpdateCheckWorker | None = None
        self._update_apply_worker: UpdateApplyWorker | None = None
        self._shutdown_panel: QWidget | None = None
        self._shutdown_label: QLabel | None = None
        self._shutdown_timer: QTimer | None = None
        # Export queue / Session history / Positive settings only make sense
        # during capture — remembers what was open so leaving and returning
        # to the capture screen restores it exactly (`_set_capture_docks_available`).
        self._capture_docks_were_visible: dict[QDockWidget, bool] = {}
        self._capture_docks_available = True  # sentinel: forces the first
        # `_set_capture_docks_available(False)` call to actually run instead
        # of no-op'ing as "already false"

        self.setWindowTitle(t("home.title"))
        self.setMinimumSize(1280, 720)

        self.home_screen = HomeScreen()
        self.home_screen.new_campaign_requested.connect(self._on_new_campaign)
        self.home_screen.open_campaign_requested.connect(self._on_open_campaign)
        self.home_screen.recent_campaign_chosen.connect(lambda p: self._open_project(Path(p)))
        self.home_screen.set_recent_projects(context.config.general.recent_projects)

        self.project_screen = ProjectScreen()
        self.project_screen.cursor_change_requested.connect(self._on_cursor_change_requested)
        self.project_screen.start_capture_requested.connect(self._on_start_capture)

        # Real background thread for exports: without it, regenerating a
        # slow export synchronously (manual crop confirm, rotation...)
        # freezes the whole window (DECISIONS.md I-92/I-98).
        self.capture_screen = CaptureScreen(
            export_executor=ThreadedExportExecutor(),
            shortcuts=self._shortcuts,
            camera_config=context.config.camera,
            persist_camera_config=lambda: save_config(context.config),
        )
        self.capture_screen.stopped.connect(self._on_capture_stopped)
        self.capture_screen.queue_changed.connect(self._refresh_export_queue_panel)
        self.capture_screen.queue_changed.connect(self._refresh_history_panel)

        self.statistics_screen = StatisticsScreen()
        self.positive_review_screen = PositiveReviewScreen()

        self._stack = QStackedWidget()
        self._stack.addWidget(self.home_screen)
        self._stack.addWidget(self.project_screen)
        self._stack.addWidget(self.capture_screen)
        self.setCentralWidget(self._stack)

        # `setVisible(False)`, not the `QDockWidget` default of visible: a
        # throwaway construction-time value would otherwise still get
        # captured as the "restore to visible" baseline the first time
        # `_set_capture_docks_available(False)` runs (`_show_home()`, still
        # inside `__init__`, before `_restore_dock_layout()`'s own
        # `restoreState()` — if there even is a saved layout — has a chance
        # to set a real one) — every dock defaulted to visible the very
        # first time capture mode was entered, restored layout or not.
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
        self.positive_settings_panel.live_changed.connect(
            lambda: self.capture_screen.refresh_active_preview(fast=True)
        )
        self.positive_settings_panel.settled_changed.connect(
            self.capture_screen.refresh_active_preview
        )
        self.positive_settings_dock = QDockWidget(t("positive_settings.title"), self)
        self.positive_settings_dock.setObjectName("positiveSettingsDock")
        self.positive_settings_dock.setWidget(self.positive_settings_panel)
        self.positive_settings_dock.setVisible(False)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.positive_settings_dock)

        self._build_menus()
        self._restore_dock_layout()
        self._show_home()
        self._reopen_last_project_if_enabled()

        if self.context.config.updates.check_enabled:
            self._start_update_check(manual=False)

    # --- menus -----------------------------------------------------------------

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        g = self._shortcuts[GLOBAL]
        self.file_menu = menu_bar.addMenu(t("menu.file"))
        self.action_new_campaign = self._add_action(
            self.file_menu, t("menu.file_new"), g["new_campaign"], self._on_new_campaign
        )
        self.action_open_campaign = self._add_action(
            self.file_menu, t("menu.file_open"), g["open_campaign"], self._on_open_campaign
        )
        self.recent_menu = self.file_menu.addMenu(t("menu.file_recent"))
        self._rebuild_recent_menu()
        self.file_menu.addSeparator()
        self.action_preferences = self._add_action(
            self.file_menu, t("menu.file_preferences"), None, self._show_preferences
        )
        self.file_menu.addSeparator()
        self.action_quit = self._add_action(
            self.file_menu, t("menu.file_quit"), g["quit"], self.close
        )

        self.project_menu = menu_bar.addMenu(t("menu.project"))
        self.action_campaign_settings = self._add_action(
            self.project_menu, t("menu.project_settings"), None, self._on_campaign_settings
        )
        csv_menu = self.project_menu.addMenu(t("menu.project_csv"))
        self.action_search_csv = self._add_action(
            csv_menu,
            t("menu.project_csv_view"),
            g["search_csv"],
            self.project_screen.focus_csv_search,
        )
        self._add_action(
            csv_menu, t("menu.project_csv_reload"), None, self.project_screen.reload_csv
        )
        self._add_action(csv_menu, t("menu.project_csv_export"), None, self._on_csv_export)
        self.action_statistics = self._add_action(
            self.project_menu, t("menu.project_statistics"), None, self._on_open_statistics
        )
        self.action_positive_review = self._add_action(
            self.project_menu,
            t("menu.project_positive_review"),
            None,
            self._on_open_positive_review,
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
            self.capture_menu, t("menu.capture_start"), g["start_capture"], self._on_start_capture
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
        self.action_go_to_name = self._add_action(
            self.capture_menu, t("menu.capture_go_to_name"), None, None
        )
        self.capture_menu.addSeparator()
        self.action_release_camera = self._add_action(
            self.capture_menu, t("menu.capture_release_camera"), None, None
        )
        for action in (
            self.action_stop_capture,
            self.action_pause_resume,
            self.action_finalize,
            self.action_reject,
            self.action_rename,
            self.action_go_to_name,
            self.action_release_camera,
        ):
            action.setEnabled(False)

        # As with the Capture menu above, no shortcut is attached here:
        # C/V/P/T are already handled by `CaptureScreen.keyPressEvent`, a
        # `QAction.setShortcut` would double-trigger on every keypress.
        self.processing_menu = menu_bar.addMenu(t("menu.processing"))
        self.action_recompute_frame = self._add_action(
            self.processing_menu, t("menu.processing_recompute_frame"), None, None
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
            self.view_menu, t("menu.view_fullscreen"), g["fullscreen"], self._toggle_fullscreen
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

        # Not a dock widget (it's an absolutely-positioned overlay on the
        # preview, not something Qt's dock system knows about), so unlike
        # the panels above there's no `visibilityChanged` to sync a
        # checkmark against — same reasoning as `action_release_camera`
        # just below: wired live in `_wire_capture_actions`, only while a
        # camera-enabled capture session is actually running.
        self.action_live_view_panel = self._add_action(
            self.view_menu, t("menu.view_live_view"), None, None
        )
        self.action_live_view_panel.setEnabled(False)

        self.help_menu = menu_bar.addMenu(t("menu.help"))
        self.action_shortcuts_help = self._add_action(
            self.help_menu, t("menu.help_shortcuts"), g["shortcuts_help"], self._show_shortcuts
        )
        self.action_check_updates = self._add_action(
            self.help_menu, t("menu.help_check_updates"), None, self._check_for_updates
        )
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
        self.action_check_updates.setEnabled(True)
        self.action_preferences.setEnabled(True)
        self._set_capture_docks_available(False)

    def _show_project(self) -> None:
        self._stack.setCurrentWidget(self.project_screen)
        self.project_menu.setEnabled(True)
        self.action_start_capture.setEnabled(True)
        self.action_check_updates.setEnabled(True)
        self.action_preferences.setEnabled(True)
        self._set_capture_docks_available(False)

    def _show_capture(self) -> None:
        self._stack.setCurrentWidget(self.capture_screen)
        self.project_menu.setEnabled(False)
        # No popup during capture (règle absolue 4): the update check result
        # is a `QMessageBox`, out of place here even on manual request.
        self.action_check_updates.setEnabled(False)
        self.action_preferences.setEnabled(False)
        self._set_capture_docks_available(True)
        self.capture_screen.setFocus()

    def _set_capture_docks_available(self, available: bool) -> None:
        """Export queue / Session history / Positive settings only belong on
        the capture screen. Elsewhere they're hidden and their View menu
        entries disabled (shown-disabled, not omitted — same convention as
        every other mode-specific menu item), remembering whatever was open
        so it comes back exactly as left.

        No-ops if already in the requested state: `_show_home()` then
        `_show_project()` both run back-to-back at startup when reopening
        the last project, and a second "not available" call would otherwise
        re-capture the docks' (already hidden) visibility, overwriting the
        real value the first call had just saved.
        """
        if available == self._capture_docks_available:
            return
        self._capture_docks_available = available
        docks = (self.export_queue_dock, self.history_dock, self.positive_settings_dock)
        actions = (self.action_export_queue, self.action_history, self.action_positive_settings)
        if available:
            for dock in docks:
                dock.setVisible(self._capture_docks_were_visible.get(dock, False))
        else:
            for dock in docks:
                # Not `dock.isVisible()`: before the main window's first
                # `show()` (startup, right after `_restore_dock_layout()`),
                # every widget reports not-visible regardless of its own
                # explicit state, since `isVisible()` also depends on its
                # ancestors actually being shown — which would silently
                # discard whatever was just restored from the last session.
                self._capture_docks_were_visible[dock] = not dock.testAttribute(
                    Qt.WidgetAttribute.WA_WState_Hidden
                )
                dock.setVisible(False)
        for action in actions:
            action.setEnabled(available)

    # --- reopen last project on startup -----------------------------------

    def _reopen_last_project_if_enabled(self) -> None:
        general = self.context.config.general
        if not general.reopen_last or not general.recent_projects:
            return
        root = Path(general.recent_projects[0])
        if not CampaignPaths(root).campaign_json.exists():
            return  # gone/moved since last time — not an error worth a startup dialog
        self._open_project(root)

    # --- preferences -------------------------------------------------------

    def _show_preferences(self) -> None:
        dialog = PreferencesDialog(
            self.context,
            app_dir=self._app_dir(),
            check_updates=self._check_for_updates,
            parent=self,
        )
        dialog.exec()
        self._apply_shortcuts()

    def _apply_shortcuts(self) -> None:
        """Reloads `context.config.shortcuts` and re-applies every binding.

        Called once the Preferences dialog closes: cheap enough (a dozen
        `QAction.setShortcut` calls plus refreshing `CaptureScreen`'s map)
        to just always redo, rather than track which ones actually changed.
        """
        self._shortcuts = merge_with_defaults(self.context.config.shortcuts)
        g = self._shortcuts[GLOBAL]
        self.action_new_campaign.setShortcut(QKeySequence(g["new_campaign"]))
        self.action_open_campaign.setShortcut(QKeySequence(g["open_campaign"]))
        self.action_quit.setShortcut(QKeySequence(g["quit"]))
        self.action_search_csv.setShortcut(QKeySequence(g["search_csv"]))
        self.action_start_capture.setShortcut(QKeySequence(g["start_capture"]))
        self.action_shortcuts_help.setShortcut(QKeySequence(g["shortcuts_help"]))
        self.action_fullscreen.setShortcut(QKeySequence(g["fullscreen"]))
        self.capture_screen.set_shortcuts(self._shortcuts)
        if self._shortcuts_window is not None:
            self._refresh_shortcuts_text()

    # --- campaign creation / opening --------------------------------------------

    def _on_new_campaign(self) -> None:
        wizard = NewCampaignWizard(self, max_name_length=self.context.config.csv.max_name_length)
        if wizard.exec() == QDialog.DialogCode.Accepted and wizard.result_campaign:
            self._open_project(wizard.result_campaign.paths.root)

    def _on_open_campaign(self) -> None:
        path = QFileDialog.getExistingDirectory(self, t("home.open_campaign"))
        if path:
            self._open_project(Path(path))

    def _open_project(self, root: Path) -> None:
        if self.capture_screen.session is not None:
            # Switching projects (new/open/recent) while a capture is
            # running would otherwise leave the old campaign's watcher and
            # export queue running in the background against a project
            # whose lock is about to be released below.
            self.capture_screen.stop_capture()

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

    # --- positive crop review -----------------------------------------------

    def _on_open_positive_review(self) -> None:
        # Same reasoning as `_on_open_statistics`: also usable outside of
        # capture, and from a campaign shared over a NAS/SMB mount opened
        # from a different machine than the one that captured it.
        session = self.capture_screen.session or self._build_offline_session()
        if session is None:
            QMessageBox.information(
                self, t("positive_review.title"), t("positive_review.unavailable")
            )
            return
        self.positive_review_screen.load(session)
        self.positive_review_screen.show()
        self.positive_review_screen.raise_()
        self.positive_review_screen.activateWindow()

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
            extra_ignored_suffixes=tuple(campaign.capture.extra_ignored_suffixes),
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
            disk_warn_gb=self.context.config.thresholds.disk_warn_gb,
            disk_critical_gb=self.context.config.thresholds.disk_critical_gb,
        )

    # --- dock layout (Export queue / Session history / Positive settings) ------

    def _restore_dock_layout(self) -> None:
        """Reapplies each panel's visibility, dock area, and floating position/size
        from the last time the app was closed. Silently keeps the default (visible,
        docked where `addDockWidget` put it) if nothing was saved yet or the saved
        state doesn't apply (corrupted value, incompatible after an upgrade)."""
        encoded = self.context.config.ui.dock_layout
        if not encoded:
            return
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return
        self.restoreState(QByteArray(raw))

    def _save_dock_layout(self) -> None:
        state = bytes(self.saveState())
        self.context.config.ui.dock_layout = base64.b64encode(state).decode("ascii")
        save_config(self.context.config)

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
        "action_rename",
        "action_go_to_name",
    )

    # Regenerate stays disabled permanently: it would duplicate Statistics ▸
    # Regenerate selection. C/V/P/T are already functional via keyboard and
    # wired here to the Processing menu.
    _PROCESSING_ACTION_SLOTS = (
        "action_recompute_frame",
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
            disk_warn_gb=self.context.config.thresholds.disk_warn_gb,
            disk_critical_gb=self.context.config.thresholds.disk_critical_gb,
            max_name_length=self.context.config.csv.max_name_length,
            export_queue_warn_threshold=self.context.config.thresholds.export_queue_warn,
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
                self.capture_screen.rename_current_image,
                self.capture_screen.open_go_to_name,
            ),
            strict=True,
        ):
            action: QAction = getattr(self, action_name)
            action.setEnabled(True)
            action.triggered.connect(slot)

        # Camera-specific: only meaningful (and only enabled) when tethered
        # capture is actually on for this campaign — unlike the actions
        # above, which apply to every capture session.
        self.action_release_camera.setEnabled(self.capture_screen.has_camera())
        if self.capture_screen.has_camera():
            self.action_release_camera.triggered.connect(
                self.capture_screen.release_camera_from_file_manager
            )

        self.action_live_view_panel.setEnabled(self.capture_screen.has_camera())
        if self.capture_screen.has_camera():
            self.action_live_view_panel.triggered.connect(
                self.capture_screen.toggle_live_view_panel_visibility
            )

        for action_name, slot in zip(
            self._PROCESSING_ACTION_SLOTS,
            (
                self.capture_screen.recompute_frame,
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
                self.capture_screen.rename_current_image,
                self.capture_screen.open_go_to_name,
            ),
            strict=True,
        ):
            action: QAction = getattr(self, action_name)
            with contextlib.suppress(TypeError, RuntimeError):
                action.triggered.disconnect(slot)
            action.setEnabled(False)

        with contextlib.suppress(TypeError, RuntimeError):
            self.action_release_camera.triggered.disconnect(
                self.capture_screen.release_camera_from_file_manager
            )
        self.action_release_camera.setEnabled(False)

        with contextlib.suppress(TypeError, RuntimeError):
            self.action_live_view_panel.triggered.disconnect(
                self.capture_screen.toggle_live_view_panel_visibility
            )
        self.action_live_view_panel.setEnabled(False)

        for action_name, slot in zip(
            self._PROCESSING_ACTION_SLOTS,
            (
                self.capture_screen.recompute_frame,
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
            layout = QVBoxLayout(window)
            layout.addWidget(make_pin_checkbox(window))
            layout.addWidget(text_edit)
            self._shortcuts_window = window
            self._shortcuts_text_edit = text_edit
            self._refresh_shortcuts_text()
        self._shortcuts_window.show()
        self._shortcuts_window.raise_()
        self._shortcuts_window.activateWindow()

    def _refresh_shortcuts_text(self) -> None:
        if self._shortcuts_text_edit is not None:
            self._shortcuts_text_edit.setPlainText(_build_shortcuts_text(self._shortcuts))

    def _show_about(self) -> None:
        QMessageBox.about(self, t("menu.help_about"), t("app.version_line", version=__version__))

    # --- updates (manual click or opt-in startup check only, never periodic) ---

    def _app_dir(self) -> Path:
        """The current installation's own directory — never a different one."""
        return Path(__file__).resolve().parents[2]

    def _check_for_updates(self) -> None:
        self._start_update_check(manual=True)

    def _start_update_check(self, *, manual: bool) -> None:
        worker = UpdateCheckWorker(self._app_dir())
        worker.finished_check.connect(lambda result: self._on_update_check_finished(result, manual))
        self._update_check_worker = worker
        worker.start()

    def _on_update_check_finished(self, result: UpdateCheckResult, manual: bool) -> None:
        self._update_check_worker = None
        if result.error is not None:
            if manual:
                message = (
                    t("update.not_git")
                    if result.error == "Not a git installation."
                    else t("update.check_failed", error=result.error)
                )
                QMessageBox.warning(self, t("update.check_title"), message)
            # Automatic (opt-in) check: fails silently — a missing network
            # is an entirely normal condition for this offline-first app,
            # not something to nag the operator about at every startup.
            return

        if not result.available:
            if manual:
                QMessageBox.information(self, t("update.check_title"), t("update.up_to_date"))
            return

        if manual:
            answer = QMessageBox.question(
                self,
                t("update.check_title"),
                t(
                    "update.available_question",
                    local=result.local_commit,
                    remote=result.remote_commit,
                ),
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._apply_update()
        else:
            self.home_screen.show_update_available(
                t("home.update_available", local=result.local_commit, remote=result.remote_commit)
            )

    def _apply_update(self) -> None:
        self.action_check_updates.setEnabled(False)
        worker = UpdateApplyWorker(self._app_dir(), sys.executable)
        worker.finished_apply.connect(self._on_update_apply_finished)
        self._update_apply_worker = worker
        worker.start()

    def _on_update_apply_finished(self, result: UpdateApplyResult) -> None:
        self._update_apply_worker = None
        self.action_check_updates.setEnabled(True)
        if result.success:
            QMessageBox.information(self, t("update.check_title"), t("update.apply_success"))
        else:
            QMessageBox.warning(
                self, t("update.check_title"), t("update.apply_failed", error=result.error)
            )

    # --- clean shutdown (processing.drain_on_exit) -----------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shutdown_panel is not None:
            # Already finalizing from a previous close attempt (e.g. a second
            # click on the window's close button): keep waiting, don't
            # restart the finalize/submit step a second time.
            event.ignore()
            return

        self._save_dock_layout()

        if self.capture_screen.session is None:
            self._release_lock()
            super().closeEvent(event)
            return

        pending = self.capture_screen.begin_shutdown()
        if pending == 0:
            # Nothing left to wait for: shut the executor thread down cleanly.
            self.capture_screen.finish_shutdown(wait_for_exports=True)
            self._release_lock()
            super().closeEvent(event)
            return
        if not self.context.config.processing.drain_on_exit:
            self.capture_screen.finish_shutdown(wait_for_exports=False)
            self._release_lock()
            super().closeEvent(event)
            return

        event.ignore()
        self._show_shutdown_panel(pending)

    def _show_shutdown_panel(self, pending: int) -> None:
        panel = QWidget()
        panel.setWindowTitle(t("shutdown.title"))
        panel.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        label = QLabel(t("shutdown.pending", count=pending))
        button = QPushButton(t("shutdown.quit_without_waiting"))
        button.clicked.connect(self._force_quit_without_waiting)
        row = QHBoxLayout()
        row.addWidget(label, stretch=1)
        row.addWidget(button)
        QVBoxLayout(panel).addLayout(row)
        panel.resize(420, 80)
        self._shutdown_panel = panel
        self._shutdown_label = label
        panel.show()
        panel.raise_()

        timer = QTimer(self)
        timer.timeout.connect(self._poll_shutdown)
        timer.start(300)
        self._shutdown_timer = timer

    def _poll_shutdown(self) -> None:
        pending = self.capture_screen.poll_export_progress()
        if pending == 0:
            self._finish_shutdown(wait_for_exports=True)
            return
        self._shutdown_label.setText(t("shutdown.pending", count=pending))

    def _force_quit_without_waiting(self) -> None:
        self._finish_shutdown(wait_for_exports=False)

    def _finish_shutdown(self, *, wait_for_exports: bool) -> None:
        if self._shutdown_timer is not None:
            self._shutdown_timer.stop()
            self._shutdown_timer = None
        if self._shutdown_panel is not None:
            self._shutdown_panel.close()
            self._shutdown_panel = None
        self.capture_screen.finish_shutdown(wait_for_exports=wait_for_exports)
        self._release_lock()
        self.close()
