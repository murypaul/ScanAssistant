"""English catalog — reference language."""

STRINGS: dict[str, str] = {
    "cli.description": "ScanAssistant — heritage photographic negative digitization assistant",
    "cli.version_help": "Show the application version and exit",
    "cli.debug_help": "Enable verbose debug logging (written to LOGS/debug.log)",
    "app.version_line": "ScanAssistant {version}",
    "cli.create_campaign_help": "Create a campaign from a CSV inventory (headless, M2)",
    "cli.capture_help": "Run a headless capture session against a watched folder (M2)",
    "cli.create_campaign_done": "Campaign created at {root}",
    "cli.create_campaign_failed": "Could not create the campaign: {error}",
    "cli.capture_missing_watched_folder": (
        "No watched folder: pass --watched-folder or set it in campaign.json"
    ),
    "cli.capture_open_failed": "Could not open the campaign: {error}",
    "cli.capture_idle": "Capture finished — no more names to assign, nothing pending.",
    "cli.capture_interrupted": "Capture interrupted — finalizing the current image…",
    "cli.capture_recovery_report": "Recovery after interruption:",
    # --- CSV viewer (06 §6) ---
    "csv.column_line": "#",
    "csv.column_name": "Name",
    "csv.column_status": "Status",
    "csv.column_source_file": "Source file",
    # --- Log tab (06 §5) ---
    "log.column_ts": "Time",
    "log.column_level": "Level",
    "log.column_type": "Type",
    "log.column_action": "Action",
    "log.column_image": "Image",
    "log.column_details": "Details",
    "log.column_result": "Result",
    # --- Home screen (06 §3) ---
    "home.title": "ScanAssistant",
    "home.new_campaign": "New campaign",
    "home.open_campaign": "Open campaign",
    "home.recent_projects": "Recent campaigns",
    "home.recent_empty": "No recent campaign yet.",
    "home.recent_entry": "{path}  —  last opened {date}",
    "home.recent_unavailable": "{path}  —  unavailable",
    "home.recent_unknown_date": "unknown date",
    "home.open_failed_title": "Could not open the campaign",
    # --- Menus (06 §12, normative labels) ---
    "menu.file": "File",
    "menu.file_new": "New campaign",
    "menu.file_open": "Open…",
    "menu.file_recent": "Recent campaigns",
    "menu.file_preferences": "Preferences…",
    "menu.file_quit": "Quit",
    "menu.project": "Project",
    "menu.project_settings": "Campaign settings…",
    "menu.project_csv": "CSV",
    "menu.project_csv_view": "View",
    "menu.project_csv_reload": "Reload",
    "menu.project_csv_export": "Export to…",
    "menu.project_statistics": "Statistics…",
    "menu.project_positive_review": "Positive crop review…",
    "statistics.title": "Statistics",
    "statistics.total": "Total: {count}",
    "statistics.done": "Done: {count}",
    "statistics.remaining": "Remaining: {count}",
    "statistics.rejected": "Rejected: {count}",
    "statistics.errors": "Errors: {count}",
    "statistics.completeness_check": "Completeness check",
    "statistics.regenerate_selection": "Regenerate selection",
    "statistics.column_name": "Name",
    "statistics.column_missing": "Missing",
    "statistics.raw_missing": "RAW file missing",
    "statistics.no_gaps": "No gaps found — every completed image has all its files.",
    "statistics.unavailable": "Statistics are unavailable: no campaign is open.",
    "positive_review.title": "Positive crop review",
    "positive_review.category_deferred": "Needs review — content frame",
    "positive_review.confirm_and_next": "Confirm && next (Enter)",
    "positive_review.nothing_to_review": "Nothing to review.",
    "positive_review.reviewing": "{name} — {index}/{total}",
    "positive_review.master_unavailable": "Master image unavailable for {name}.",
    "positive_review.unavailable": "Positive crop review is unavailable: no campaign is open.",
    "export_queue.title": "Export queue",
    "export_queue.column_name": "Name",
    "export_queue.column_kind": "Kind",
    # --- Shutdown while exports are pending (processing.drain_on_exit) ---
    "shutdown.title": "Finalizing",
    "shutdown.pending": "Finalizing: {count} export(s) pending…",
    "shutdown.quit_without_waiting": "Quit without waiting",
    # --- Preferences dialog (global config.json, File ▸ Preferences…) ---
    "preferences.title": "Preferences",
    "preferences.tab_general": "General",
    "preferences.tab_processing": "Processing",
    "preferences.tab_thresholds": "Thresholds",
    "preferences.tab_updates": "Updates",
    "preferences.tab_camera": "Camera",
    "preferences.tab_shortcuts": "Shortcuts",
    "preferences.reopen_last": "Reopen last project on startup",
    "preferences.exiftool": "exiftool path",
    "preferences.exiftool_tooltip": (
        "Leave empty to search automatically on PATH. Only needed if exiftool"
        " isn't found there — without it, exports still work but carry no"
        " EXIF/IPTC/XMP metadata."
    ),
    "preferences.test": "Test",
    "preferences.drain_on_exit": "Wait for pending exports before closing",
    "preferences.drain_on_exit_tooltip": (
        "On: closing waits for TIFF/JPEG conversions still in progress,"
        " showing their count, with a Quit without waiting option. Off:"
        " closes immediately — nothing is lost, pending conversions simply"
        " resume the next time this campaign is opened."
    ),
    "preferences.max_name_length": "Maximum name length",
    "preferences.max_name_length_tooltip": (
        "Longest negative name accepted when importing a new CSV. Existing"
        " campaigns stay readable regardless of this setting."
    ),
    "preferences.disk_warn": "Disk space warning",
    "preferences.disk_warn_tooltip": (
        "Below this much free space, a warning banner appears — capture keeps running."
    ),
    "preferences.disk_critical": "Disk space critical",
    "preferences.disk_critical_tooltip": (
        "Below this much free space, processing is suspended until space is"
        " freed and the operator resumes it. Must be lower than the warning"
        " threshold."
    ),
    "preferences.export_queue_warn": "Export queue warning size",
    "preferences.export_queue_warn_tooltip": (
        "Above this many pending exports, a warning banner appears — the"
        " queue keeps draining normally, this is an early warning only."
    ),
    "preferences.updates_check_enabled": "Check for updates at startup",
    "preferences.updates_check_enabled_tooltip": (
        "The only network access this app ever makes: a single git fetch"
        " when the app opens, to compare against the tracked remote. Nothing"
        " is sent beyond that; a manual check (Help ▸ Check for updates…) is"
        " always available regardless of this setting."
    ),
    "preferences.camera_enabled": "Enable tethered camera (remote trigger + live view)",
    "preferences.camera_enabled_tooltip": (
        "Nikon D750 over USB, first supported body. Adds a remote-trigger"
        " shortcut and a live view vignette to capture mode — the RAW still"
        " arrives through the watched folder like any other capture. Off by"
        " default: leaves everything else unaffected."
    ),
    "preferences.camera_enabled_restart_note": (
        "Takes effect the next time ScanAssistant is started."
    ),
    "preferences.camera_install_title": "Tethered camera support",
    "preferences.camera_install_question": (
        "Tethered capture needs an extra Python package (gphoto2) that isn't"
        " installed yet. Install it now? This downloads it from PyPI into"
        " ScanAssistant's own virtual environment — nothing else changes."
    ),
    "preferences.camera_install_success": (
        "Installed. Tethered camera support is now enabled."
    ),
    "preferences.camera_install_failed": (
        "Could not install the camera package: {error}"
    ),
    "preferences.export_settings": "Export settings…",
    "preferences.import_settings": "Import settings…",
    "preferences.restart_notice": "Settings imported. Restart ScanAssistant to fully apply them.",
    "preferences.shortcuts_press_key": "Press a key…",
    "preferences.shortcuts_reset": "Reset",
    "preferences.shortcuts_reset_all": "Reset all to defaults",
    "preferences.shortcuts_invalid_key": (
        "{shortcut} can't be used — only letters, digits, function keys, arrows,"
        " Enter, Escape, Space, Tab (with modifiers) are allowed, and Ctrl+S never is."
    ),
    "preferences.shortcuts_conflict": "{shortcut} is already used by “{action}” in this context.",
    "preferences.shortcuts_context_capture": "Capture",
    "preferences.shortcuts_context_name_conflict": "Name conflict",
    "preferences.shortcuts_context_global": "Global",
    "preferences.shortcut_finalize": "Finalize the current image",
    "preferences.shortcut_reject": "Reject the current image",
    "preferences.shortcut_rotate": "Rotate 90°",
    "preferences.shortcut_recompute_frame": "Recompute frame",
    "preferences.shortcut_positive_preview": "Positive preview",
    "preferences.shortcut_master_preview": "Master preview",
    "preferences.shortcut_cycle_preview": "Cycle preview (negative → positive → master)",
    "preferences.shortcut_go_to_name": "Go to name",
    "preferences.shortcut_trigger_capture": "Trigger the camera remotely",
    "preferences.shortcut_pause_resume": "Pause / Resume",
    "preferences.shortcut_toggle_live_view": "Toggle live view",
    "preferences.shortcut_stop_capture": "Stop capture",
    "preferences.shortcut_toggle_guides": "Toggle rule-of-thirds guides",
    "preferences.shortcut_option_1": "Rename current image",
    "preferences.shortcut_option_2": "Replace existing",
    "preferences.shortcut_option_3": "Rename existing",
    "preferences.shortcut_new_campaign": "New campaign",
    "preferences.shortcut_open_campaign": "Open a campaign",
    "preferences.shortcut_quit": "Quit",
    "preferences.shortcut_search_csv": "Search in the CSV viewer",
    "preferences.shortcut_start_capture": "Start capture",
    "preferences.shortcut_shortcuts_help": "Keyboard shortcuts help",
    "preferences.shortcut_fullscreen": "Full screen",
    "history.title": "Session history",
    "positive_settings.title": "Positive settings",
    "menu.project_open_folder": "Open campaign folder",
    "menu.project_today_log": "Today's log",
    "menu.capture": "Capture",
    "menu.capture_start": "Start capture",
    "menu.capture_stop": "Stop",
    "menu.capture_pause_resume": "Pause / Resume",
    "menu.capture_finalize": "Finalize current image",
    "menu.capture_reject": "Reject current image",
    "menu.capture_rename": "Rename current image…",
    "menu.capture_go_to_name": "Go to name…",
    "menu.capture_release_camera": "Release camera from file manager",
    "menu.processing": "Processing",
    "menu.processing_recompute_frame": "Recompute frame",
    "menu.processing_rotate": "Rotate 90°",
    "menu.processing_positive_preview": "Positive preview",
    "menu.processing_master_preview": "Master preview",
    "menu.processing_regenerate": "Regenerate current image exports",
    "menu.metadata": "Metadata",
    "menu.metadata_campaign_iptc": "Campaign IPTC…",
    "menu.metadata_check_exiftool": "Check exiftool",
    "metadata.exiftool_available": "exiftool is available — derivatives will carry metadata.",
    "metadata.exiftool_unavailable": (
        "exiftool not found — exports will be written without metadata."
    ),
    "menu.view": "View",
    "menu.view_fullscreen": "Full screen",
    "menu.view_brightness": "Interface brightness",
    "menu.view_brightness_normal": "Normal",
    "menu.view_brightness_dimmed": "Dimmed",
    "menu.view_brightness_minimal": "Minimal",
    "menu.view_export_queue": "Export queue",
    "menu.view_history": "Session history",
    "menu.view_positive_settings": "Positive settings",
    "menu.help": "Help",
    "menu.help_shortcuts": "Keyboard shortcuts",
    "menu.help_check_updates": "Check for updates…",
    "menu.help_about": "About",
    # --- Updates (I-102: manual or opt-in-at-startup only, never periodic) ---
    "update.check_title": "Check for updates",
    "update.up_to_date": "ScanAssistant is up to date.",
    "update.not_git": (
        "This installation wasn't set up with git — reinstall using "
        "install.sh/install.ps1 to update."
    ),
    "update.check_failed": "Could not check for updates: {error}",
    "update.available_question": "An update is available ({local} → {remote}). Update now?",
    "update.apply_success": "Update applied. Restart ScanAssistant to use the new version.",
    "update.apply_failed": "Update failed: {error}",
    "home.update_available": (
        "An update is available ({local} → {remote}) — Help ▸ Check for updates."
    ),
    # --- Capture screen (06 §8, M4) ---
    "capture.preview_ready_next": "Ready — next: {name}",
    "capture.preview_copying": "{name} — copying…",
    "capture.preview_unavailable": "Preview unavailable (unreadable RAW): {error}",
    "capture.progress": "{done}/{total} · {pct}%",
    "capture.next": "next: {name}",
    "capture.queue": "export queue: {count}",
    "capture.mode_capture": "● CAPTURE",
    "capture.mode_pause": "● PAUSE",
    "capture.status_rejected": "Rejected {name}",
    "capture.status_conflict": "Name conflict: {name} already exists",
    "capture.conflict_title": "Name conflict: {name} already exists — choose 1, 2, or 3.",
    "capture.conflict_option1": "1 — Rename current image",
    "capture.conflict_use_next_free": "Use next free name",
    "capture.conflict_option2": "2 — Replace existing",
    "capture.conflict_option3": "3 — Rename existing file",
    "capture.status_export_done": "Exports complete ({name})",
    "capture.status_rotation": "Rotation: {rotation_deg}°",
    "capture.status_reopened": "Reopened {name} for correction",
    "capture.status_reopen_busy": "Finalize or reject the current image before reopening another",
    "capture.status_unknown_name": "Unknown or already-used name: {name}",
    "capture.dismiss_warning": "Dismiss",
    "capture.status_image_errored": "{name} flagged as error ({code}) — see Statistics to retry.",
    "capture.camera_release_status": "Released camera from file manager, reconnecting…",
    "capture.resume_processing": "Resume processing",
    "capture.recovery_report_title": "Recovery after interruption",
    "capture.go_to_name_placeholder": "Go to name… (Enter to jump, Escape to cancel)",
    "capture.confidence_reliable": "● RELIABLE {score}",
    "capture.confidence_review": "● TO CHECK {score}",
    "capture.confidence_impossible": "● IMPOSSIBLE {score}",
    "capture.confidence_manual": "● MANUAL",
    "capture.cannot_start_title": "Cannot start capture",
    "capture.error_csv_exhausted": (
        "No name left in the inventory. Add rows to the CSV or finish the campaign."
    ),
    "capture.error_watched_folder_missing": "The watched folder is not set or does not exist.",
    "capture.error_watched_folder_inaccessible": "The watched folder is not accessible: {error}",
    # --- Live view / remote trigger (tethered camera) ---
    "live_view.live_badge": "LIVE",
    "live_view.capturing": "Capturing…",
    "live_view.fps_unlimited": "Unlimited",
    "live_view.fps_label": "FPS",
    "live_view.fps_measured": "{fps:.0f} fps",
    "live_view.fps_measured_pending": "— fps",
    "live_view.opacity_label": "Opacity",
    "live_view.toggle_tooltip": "Toggle live view (L)",
    "live_view.expand_tooltip": "Click to check focus in detail",
    "live_view.collapse_tooltip": "Back to thumbnail",
    # --- New campaign wizard (06 §4) ---
    "wizard.title": "New campaign",
    "wizard.browse": "Browse…",
    "wizard.creation_failed_title": "Could not create the campaign",
    "common.horizontal": "Horizontal",
    "common.vertical": "Vertical",
    "common.pin_on_top": "Keep on top",
    "wizard.step1.title": "Identity",
    "wizard.step1.subtitle": "Name and describe this campaign.",
    "wizard.step1.name": "Name",
    "wizard.step1.description": "Description",
    "wizard.step1.operator": "Operator",
    "wizard.step1.institution": "Institution",
    "wizard.step1.negative_format": "Negative format (documentary label)",
    "wizard.step1.clone_button": "Clone settings from an existing campaign…",
    "wizard.step1.clone_browse_title": "Select an existing campaign folder",
    "wizard.step1.clone_failed_title": "Could not clone settings",
    "wizard.step1.clone_applied": "Settings cloned from {name}.",
    "wizard.step2.title": "Folders",
    "wizard.step2.subtitle": "Where the campaign is stored, and where the camera drops files.",
    "wizard.step2.location": "Campaign location",
    "wizard.step2.campaign_folder_preview": "Campaign folder: {root}",
    "wizard.step2.watched_folder": "Watched folder",
    "wizard.step2.watched_folder_help": (
        "Files dropped here are moved into the campaign once verified "
        "(single copy, never duplicated)."
    ),
    "wizard.step3.title": "CSV inventory",
    "wizard.step3.subtitle": "Select the ordered list of names to assign to incoming images.",
    "wizard.step3.csv_path": "CSV file",
    "wizard.step3.csv_browse": "Select a CSV file",
    "wizard.step3.name_column": "Name column",
    "wizard.step3.has_header": "First row is a header (uncheck for a plain list of names)",
    "wizard.step3.preview_label": "Preview (first 20 rows):",
    "wizard.step3.validation_ok": "{rows} row(s) ready to import.",
    "wizard.step3.validation_warnings": " ({count} warning(s))",
    "wizard.step3.validation_errors": "{count} problem(s) found — fix the CSV and retry:",
    "wizard.step4.title": "Shooting and cropping",
    "wizard.step4.subtitle": "Default orientation and cropping behavior for this campaign.",
    "wizard.step4.framing_enabled": "Automatic frame detection enabled",
    "wizard.step4.default_orientation": "Default orientation",
    "wizard.step4.size_mode": "Dimensions mode",
    "wizard.step4.size_mode_native": "Native (each negative keeps its detected size)",
    "wizard.step4.size_mode_fixed": "Fixed (constant final size)",
    "wizard.step4.final_width": "Final width (px)",
    "wizard.step4.final_height": "Final height (px)",
    "wizard.step4.margin_pct": "Margin",
    "wizard.step5.title": "Exports",
    "wizard.step5.subtitle": "Master and positive export settings.",
    "wizard.step5.enabled": "Enabled",
    "wizard.step5.tiff_group": "TIFF master",
    "wizard.step5.jpeg_master_group": "JPEG master",
    "wizard.step5.jpeg_positive_group": "JPEG reading positive",
    "wizard.step5.bits": "Bit depth",
    "wizard.step5.compression": "Compression",
    "wizard.step5.compression_none": "None",
    "wizard.step5.compression_lzw": "LZW",
    "wizard.step5.colorspace": "Color space",
    "wizard.step5.colorspace_srgb": "sRGB (color)",
    "wizard.step5.colorspace_gray": "Grayscale",
    "wizard.step5.quality": "JPEG quality",
    "wizard.step5.long_edge": "Long edge (px)",
    "wizard.step5.long_edge_full": "Full size",
    "wizard.step5.mode": "Positive mode",
    "wizard.step5.mode_simple": "Simple",
    "wizard.step5.mode_auto": "Automatic",
    "wizard.step5.mode_manual": "Manual",
    "wizard.step5.horizontal_flip": "Flip horizontally (reading orientation)",
    "wizard.step5.suffix": "Filename suffix",
    "wizard.step5.manual_group": "Manual mode settings",
    "wizard.step5.exposure_ev": "Exposure (EV)",
    "wizard.step5.contrast": "Contrast",
    "wizard.step5.shadows": "Shadows",
    "wizard.step5.highlights": "Highlights",
    "wizard.step6.title": "IPTC metadata",
    "wizard.step6.subtitle": "Written to every export.",
    "wizard.step6.creator": "Creator",
    "wizard.step6.institution": "Institution",
    "wizard.step6.copyright": "Copyright",
    "wizard.step6.collection": "Collection",
    "wizard.step6.keywords": "Keywords",
    "wizard.step6.keywords_placeholder": "Comma-separated",
    "wizard.step7.title": "Summary",
    "wizard.step7.subtitle": "Review before creating the campaign.",
    "wizard.step7.create_button": "Create campaign",
    # --- Project screen (06 §5-6) ---
    "project.tab_summary": "Summary",
    "project.tab_folders": "Folders",
    "project.tab_capture": "Capture",
    "project.tab_framing": "Framing",
    "project.tab_exports": "Exports",
    "project.tab_metadata": "Metadata",
    "project.tab_csv": "CSV",
    "project.tab_log": "Log",
    "project.invalid_setting_title": "Invalid setting",
    "project.summary_counts": "{done} done · {remaining} remaining · {total} total",
    "project.summary_root": "Campaign folder: {root}",
    "project.open_campaign_folder": "Open campaign folder",
    "project.open_watched_folder": "Open watched folder",
    "project.export_group": "Export copies",
    "project.export_destination": "Destination folder",
    "project.export_layout": "Layout",
    "project.export_layout_flat": "All files in one folder",
    "project.export_layout_by_type": "One subfolder per file type",
    "project.export_button": "Export now",
    "project.export_no_destination_title": "No destination",
    "project.export_no_destination": "Choose a destination folder first.",
    "project.export_done_title": "Export complete",
    "project.export_done": "{copied} file(s) copied, {skipped} already present (skipped).",
    "project.extensions": "RAW extensions",
    "project.watch_mode": "Watch mode",
    "project.watch_mode_auto": "Automatic",
    "project.watch_mode_native": "Native (watchdog)",
    "project.watch_mode_polling": "Polling",
    "project.stabilization_delay": "Stabilization delay",
    "project.stabilization_timeout": "Stabilization timeout",
    "project.verify_checksum": "Verify SHA-256 on cross-volume ingestion",
    "project.extra_ignored_suffixes": "Additional ignored file suffixes",
    "project.extra_ignored_suffixes_tooltip": (
        "Added on top of the built-in ignore list (.tmp, .part, .crdownload,"
        " hidden files) — never replaces it. Use only for a junk-file pattern"
        " your camera or card software actually produces: a suffix that"
        " matches a real capture would hide it from the watched folder."
    ),
    "project.csv_search_placeholder": "Search by name (Ctrl+F)",
    "project.csv_filter_all": "All statuses",
    "project.csv_filter_todo": "Todo",
    "project.csv_filter_done": "Done",
    "project.csv_set_cursor_here": "Set cursor here",
    "project.log_filter_all_types": "All types",
    "project.log_filter_all_levels": "All levels",
    "project.open_logs_folder": "Open LOGS folder",
    # --- Error catalog (09_ERREURS_ET_ROBUSTESSE.md §3, normative wording) ---
    "error.E-01_warning": "Low disk space: {free_gb:.1f} GB free.",
    "error.E-01_critical": (
        "Insufficient disk space ({free_gb:.1f} GB). "
        "Processing suspended — free up space, then resume."
    ),
    "error.E-02": "The campaign folder is unreachable. Check the storage, then resume.",
    "error.E-03": (
        "File {path} never stabilized (interrupted copy?). It will be retried on its next change."
    ),
    "error.E-04": "Integrity check failed for {name}. Copy restarted.",
    "error.E-07": "The watched folder is unreachable. Capture suspended.",
    "error.E-08": (
        "Could not remove {name} from the watched folder. "
        "It was marked as processed and will be skipped."
    ),
    "error.E-10": "Invalid campaign file: {field} — {detail}.",
    "error.E-11": "The CSV cannot be imported: {count} problem(s).",
    "error.E-12": "No name left in the inventory. Add rows to the CSV or finish the campaign.",
    "error.E-13": "The inventory was modified by another program.",
    "error.E-14": "This campaign is already open (host {host}, PID {pid}).",
    "error.E-15": "The processing queue is growing ({queue_size} pending).",
    "error.E-16": "The inventory file (inventory.csv) is missing from this campaign folder.",
    "error.E-16_backup_hint": (
        " A backup exists (inventory.csv.bak), but it reflects an earlier state, "
        "before any progress was recorded — check it before restoring it."
    ),
    "error.E-17": "Camera not detected. Check the USB cable and that it is powered on.",
    "error.E-18": (
        "The camera's USB connection is in use by another program "
        "(on Linux Mint, check whether gvfs is holding it) — close it and try again."
    ),
    "error.E-19": (
        "The camera looks like a USB drive rather than a camera — "
        "switch its USB mode to PTP/MTP in its Setup menu."
    ),
    "error.E-20": "The remote trigger failed. Check the camera and try again.",
    "error.E-21": "Live view could not be started. Turn the camera off and on, then try again.",
    "error.E-22": (
        "The camera reported a capture, but no file arrived (memory card full or missing?)."
    ),
    "error.generic": "Internal error — see LOGS/debug.log.",
}
