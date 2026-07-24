"""Writes derivative metadata, exiftool backend.

`MetadataWriter` isolates `pyexiftool`/exiftool behind a minimal
interface (a test double can stand in). A failure is **non-blocking**:
the caller logs `METADATA/missing` and continues — the pixel export
stays valid. RAW files are never modified: this module only writes to
derivatives (`derivative_path`), never to `raw_path`.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scanassistant import __version__
from scanassistant.journal.techlog import get_logger
from scanassistant.project.campaign import IptcConfig

CREATOR_TOOL = f"ScanAssistant {__version__}"

# The exiftool subprocess/pipe exchange (`pyexiftool`) has no call-level
# timeout of its own — confirmed in real use to be able to hang
# indefinitely (desynced stdin/stdout handshake, a stuck subprocess) rather
# than raise. Whichever export worker calls `write()` (the single-worker
# master queue or a `positive_finalize` pool worker) would then never come
# back for any *later* task either, since nothing else drains that queue —
# every image after the stuck one silently stops arriving in the app, with
# no error shown, indistinguishable from the whole capture pipeline having
# frozen. Bounding it here turns that into exactly the "failure is
# non-blocking" contract this module already documents, instead of an
# unbounded wait no caller can protect itself against.
_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ProductionInfo:
    """Production traceability: processing detail lives in the journal."""

    name: str
    source_file: str


class MetadataWriter(Protocol):
    """Writes a derivative's metadata."""

    def write(
        self,
        raw_path: Path,
        derivative_path: Path,
        *,
        iptc: IptcConfig,
        production: ProductionInfo,
    ) -> None: ...


def is_available(executable: str = "") -> bool:
    """Checks whether exiftool is available on the PATH."""
    return bool(shutil.which(executable or "exiftool"))


class ExifToolMetadataWriter:
    """Production implementation (`pyexiftool`)."""

    def __init__(self, *, executable: str = "") -> None:
        self._executable = executable or None

    def write(
        self,
        raw_path: Path,
        derivative_path: Path,
        *,
        iptc: IptcConfig,
        production: ProductionInfo,
    ) -> None:
        """Runs the actual exiftool exchange on its own daemon thread, bounded
        by `_TIMEOUT_S` — see this module's own top-of-file rationale. On
        timeout, that thread (and whatever stuck subprocess it's still
        waiting on) is simply abandoned rather than joined: nothing here
        ever blocks the caller for longer than `_TIMEOUT_S`, matching the
        "failure is non-blocking" contract regardless of what the stuck
        exiftool call is actually doing."""
        outcome: dict[str, BaseException] = {}

        def _run() -> None:
            try:
                self._write_now(raw_path, derivative_path, iptc=iptc, production=production)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's own thread below
                outcome["error"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(_TIMEOUT_S)
        if thread.is_alive():
            get_logger().warning(
                "exiftool metadata write for %s timed out after %.0fs — abandoning",
                derivative_path,
                _TIMEOUT_S,
            )
            raise TimeoutError(f"exiftool metadata write timed out after {_TIMEOUT_S:.0f}s")
        if "error" in outcome:
            raise outcome["error"]

    def _write_now(
        self,
        raw_path: Path,
        derivative_path: Path,
        *,
        iptc: IptcConfig,
        production: ProductionInfo,
    ) -> None:
        import exiftool

        with exiftool.ExifToolHelper(executable=self._executable) as et:
            et.execute(
                "-overwrite_original",
                "-TagsFromFile",
                str(raw_path),
                "-EXIF:all",
                str(derivative_path),
            )
            et.set_tags(
                [str(derivative_path)],
                tags=_tags(iptc, production),
                params=["-overwrite_original"],
            )


def _tags(iptc: IptcConfig, production: ProductionInfo) -> dict[str, object]:
    """EXIF fixes + descriptive IPTC/XMP + production XMP."""
    tags: dict[str, object] = {
        "EXIF:Orientation": 1,
        "EXIF:ThumbnailImage": "",
        "XMP-xmp:CreatorTool": CREATOR_TOOL,
        "XMP-dc:Identifier": production.name,
        "XMP-dc:Source": production.source_file,
        "IPTC:ObjectName": production.name,
        "XMP-dc:Title": production.name,
    }
    if iptc.creator:
        tags["IPTC:By-line"] = iptc.creator
        tags["XMP-dc:Creator"] = iptc.creator
    if iptc.copyright:
        tags["IPTC:CopyrightNotice"] = iptc.copyright
        tags["XMP-dc:Rights"] = iptc.copyright
    if iptc.institution:
        tags["IPTC:Credit"] = iptc.institution
        tags["XMP-photoshop:Credit"] = iptc.institution
    if iptc.collection:
        tags["IPTC:Source"] = iptc.collection
        tags["XMP-photoshop:Source"] = iptc.collection
    if iptc.keywords:
        tags["IPTC:Keywords"] = list(iptc.keywords)
        tags["XMP-dc:Subject"] = list(iptc.keywords)
    return tags
