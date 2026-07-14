"""Entry point for `python -m scanassistant`.

Two headless subcommands drive a campaign without the GUI:

- `create-campaign`: creates a campaign from a CSV (minimal, wizard-free
  equivalent of the graphical flow).
- `capture`: opens a campaign and runs `core.session.CaptureSession`
  until the CSV is exhausted (or Ctrl+C), with the real export pipeline
  (`core.export_runner.MasterExportRunner`).

Launching the GUI also goes through this entry point.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from scanassistant import __version__
from scanassistant.core.crash_recovery import perform_crash_recovery
from scanassistant.core.events import (
    CriticalError,
    ImageIngested,
    ImageRejected,
    ImageStateChanged,
    NameConflictDetected,
    StabilizationTimedOut,
)
from scanassistant.core.events import Warning as WarningEvent
from scanassistant.core.export_runner import MasterExportRunner
from scanassistant.core.fs import RealFileSystem
from scanassistant.core.session import CaptureSession, SessionEvent
from scanassistant.i18n import t
from scanassistant.imaging.raw import RawpyDecoder
from scanassistant.journal import techlog
from scanassistant.journal.journal import Journal
from scanassistant.metadata.writer import ExifToolMetadataWriter
from scanassistant.project.campaign import Campaign, OpenedCampaign, create_campaign, open_campaign
from scanassistant.project.errors import ScanAssistantError
from scanassistant.project.lock import acquire_lock
from scanassistant.watcher.monitor import FolderMonitor
from scanassistant.watcher.stability import poll_interval_s


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scanassistant", description=t("cli.description"))
    parser.add_argument("--version", action="store_true", help=t("cli.version_help"))
    parser.add_argument("--debug", action="store_true", help=t("cli.debug_help"))

    subparsers = parser.add_subparsers(dest="command")

    create = subparsers.add_parser("create-campaign", help=t("cli.create_campaign_help"))
    create.add_argument("--root", required=True, type=Path)
    create.add_argument("--name", required=True)
    create.add_argument("--csv", required=True, type=Path)
    create.add_argument("--watched-folder", required=True, type=Path)
    create.add_argument("--extensions", default=".nef", help="Comma-separated list, e.g. .nef,.cr2")
    create.add_argument("--no-verify-checksum", action="store_true")

    capture = subparsers.add_parser("capture", help=t("cli.capture_help"))
    capture.add_argument("--project", required=True, type=Path)
    capture.add_argument("--watched-folder", type=Path, default=None)
    capture.add_argument("--poll-interval", type=float, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(t("app.version_line", version=__version__))
        return 0

    techlog.setup_logging(debug=args.debug)
    logger = techlog.get_logger()
    logger.info("SYSTEM/started version=%s", __version__)

    if args.command == "create-campaign":
        return _run_create_campaign(args)
    if args.command == "capture":
        return _run_capture(args)

    from scanassistant.gui.app import run_gui

    return run_gui()


def _run_create_campaign(args: argparse.Namespace) -> int:
    campaign = Campaign(name=args.name)
    campaign.capture.watched_folder = str(args.watched_folder)
    campaign.capture.extensions = [e.strip() for e in args.extensions.split(",") if e.strip()]
    campaign.capture.verify_checksum = not args.no_verify_checksum

    try:
        create_campaign(args.root, campaign, args.csv)
    except ScanAssistantError as exc:
        print(t("cli.create_campaign_failed", error=str(exc)))
        return 1

    print(t("cli.create_campaign_done", root=str(args.root)))
    return 0


def _run_capture(args: argparse.Namespace) -> int:
    try:
        opened = open_campaign(args.project)
    except (ScanAssistantError, OSError) as exc:
        print(t("cli.capture_open_failed", error=str(exc)))
        return 1

    try:
        lock = acquire_lock(opened.paths.lock_file)
    except ScanAssistantError as exc:
        print(t("cli.capture_open_failed", error=str(exc)))
        return 1

    try:
        return _drive_capture(args, opened, was_stale=lock.was_stale)
    finally:
        lock.release()


def _drive_capture(args: argparse.Namespace, opened: OpenedCampaign, *, was_stale: bool) -> int:
    watched_folder = args.watched_folder or (
        Path(opened.campaign.capture.watched_folder)
        if opened.campaign.capture.watched_folder
        else None
    )
    if watched_folder is None:
        print(t("cli.capture_missing_watched_folder"))
        return 1

    journal = Journal(opened.paths.logs_dir)
    monitor = FolderMonitor(
        watched_folder,
        opened.campaign.capture.extensions,
        # Always "polling" here: this headless loop drives `tick()` itself
        # without starting `FolderMonitor.start()`'s real watchdog thread —
        # native mode, which assumes that thread, is reserved for the GUI.
        watch_mode="polling",
        stabilization_delay_s=opened.campaign.capture.stabilization_delay_s,
        stabilization_timeout_s=opened.campaign.capture.stabilization_timeout_s,
    )
    session = CaptureSession(
        paths=opened.paths,
        campaign=opened.campaign,
        inventory=opened.inventory,
        state=opened.state,
        journal=journal,
        fs=RealFileSystem(),
        monitor=monitor,
        export_runner=MasterExportRunner(
            decoder=RawpyDecoder(),
            campaign=opened.campaign,
            paths=opened.paths,
            metadata_writer=ExifToolMetadataWriter(),
            journal=journal,
        ),
    )
    if was_stale:
        report = perform_crash_recovery(session)
        print(t("cli.capture_recovery_report"))
        for line in report.summary_lines():
            print(f"  - {line}")
    session.initial_scan()  # ignores pre-existing files by default

    poll_interval = args.poll_interval or poll_interval_s(
        opened.campaign.capture.stabilization_delay_s
    )

    try:
        while True:
            for event in session.pump(time.monotonic()):
                _print_event(event)
            if session.is_idle:
                print(t("cli.capture_idle"))
                break
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print(t("cli.capture_interrupted"))
    finally:
        session.stop()

    return 0


def _print_event(event: SessionEvent) -> None:
    """Minimal display (the GUI has its own localized capture screen)."""
    if isinstance(event, ImageIngested):
        print(f"ingested: {event.name} (from {event.source_file})")
    elif isinstance(event, ImageStateChanged):
        print(f"{event.name}: {event.previous} -> {event.new}")
    elif isinstance(event, ImageRejected):
        print(f"rejected: {event.name}")
    elif isinstance(event, NameConflictDetected):
        print(f"conflict: {event.name} already exists ({event.existing_path})")
    elif isinstance(event, StabilizationTimedOut):
        print(f"warning: {event.path} never stabilized (E-03)")
    elif isinstance(event, WarningEvent):
        print(f"warning {event.code}: {event.details}")
    elif isinstance(event, CriticalError):
        print(f"critical {event.code}: {event.details}")


if __name__ == "__main__":
    sys.exit(main())
