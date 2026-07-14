"""Writes derivative metadata, exiftool backend.

`MetadataWriter` isolates `pyexiftool`/exiftool behind a minimal
interface (a test double can stand in). A failure is **non-blocking**:
the caller logs `METADATA/missing` and continues — the pixel export
stays valid. RAW files are never modified: this module only writes to
derivatives (`derivative_path`), never to `raw_path`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scanassistant import __version__
from scanassistant.project.campaign import IptcConfig

CREATOR_TOOL = f"ScanAssistant {__version__}"


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
