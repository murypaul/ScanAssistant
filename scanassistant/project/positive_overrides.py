"""Durable per-image overrides from the positive-review screen.

`CaptureSession.apply_manual_positive_override` only threads the operator's
choice through the one regeneration it triggers (`ExportContext`, in
memory) — without this, a later unrelated regeneration of the same image
(crash recovery, a retried export) falls back to the automatic content
frame and silently discards what the operator confirmed. This module is
the durable side of that choice, read by `core.recovery.rebuild_export_context`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import Any

from scanassistant.core.fs import FileSystem
from scanassistant.project.layout import CampaignPaths

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PositiveOverride:
    content_frame: tuple[float, float, float, float] | None = None  # x, y, w, h fractions
    settings: tuple[float, int, int, int] | None = (
        None  # exposure_ev, contrast, shadows, highlights — legacy engine only
    )
    # print_engine manual overrides (13_INVERSION_NEGATIFS.md §9, DECISIONS.md
    # I-181): a parallel, independent set of fields — never converted to or
    # from `settings` above, the two engines' parameter spaces don't
    # correspond 1:1. Each `None` means that group stays automatic.
    print_dmin: tuple[float, float, float] | None = None
    print_exposure_shift: float | None = None
    print_contrast: float | None = None
    print_paper_black: float | None = None
    print_paper_soft_clip: float | None = None


def load_positive_overrides(paths: CampaignPaths, fs: FileSystem) -> dict[str, PositiveOverride]:
    """Empty if `positive_overrides.json` doesn't exist yet (no manual override so far)."""
    if not fs.exists(paths.positive_overrides_json):
        return {}
    data = json.loads(fs.read_text(paths.positive_overrides_json))
    overrides: dict[str, PositiveOverride] = {}
    for name, entry in data.get("overrides", {}).items():
        content_frame = entry.get("content_frame")
        settings = entry.get("settings")
        print_dmin = entry.get("print_dmin")
        overrides[name] = PositiveOverride(
            content_frame=tuple(content_frame) if content_frame is not None else None,  # type: ignore[arg-type]
            settings=tuple(settings) if settings is not None else None,  # type: ignore[arg-type]
            print_dmin=tuple(print_dmin) if print_dmin is not None else None,  # type: ignore[arg-type]
            print_exposure_shift=entry.get("print_exposure_shift"),
            print_contrast=entry.get("print_contrast"),
            print_paper_black=entry.get("print_paper_black"),
            print_paper_soft_clip=entry.get("print_paper_soft_clip"),
        )
    return overrides


def _save_positive_overrides(
    overrides: dict[str, PositiveOverride], paths: CampaignPaths, fs: FileSystem
) -> None:
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "overrides": {name: asdict(override) for name, override in overrides.items()},
    }
    fs.write_text(
        paths.positive_overrides_json, json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    )


def set_positive_override(
    paths: CampaignPaths,
    fs: FileSystem,
    name: str,
    *,
    content_frame: tuple[float, float, float, float] | None,
    settings: tuple[float, int, int, int] | None,
) -> None:
    """Records (or replaces) the crop/legacy-engine override for `name`,
    read-modify-write — preserves any existing print_engine override
    (`set_positive_print_overrides`) for the same image, a separate
    concern (DECISIONS.md I-181)."""
    overrides = load_positive_overrides(paths, fs)
    existing = overrides.get(name, PositiveOverride())
    overrides[name] = replace(existing, content_frame=content_frame, settings=settings)
    _save_positive_overrides(overrides, paths, fs)


def set_positive_print_overrides(
    paths: CampaignPaths,
    fs: FileSystem,
    name: str,
    *,
    dmin: tuple[float, float, float] | None,
    exposure_shift: float | None,
    contrast: float | None,
    paper_black: float | None,
    paper_soft_clip: float | None,
) -> None:
    """Records (or replaces) the print_engine override for `name`,
    read-modify-write — preserves any existing crop/legacy-engine override
    (`set_positive_override`) for the same image (DECISIONS.md I-181)."""
    overrides = load_positive_overrides(paths, fs)
    existing = overrides.get(name, PositiveOverride())
    overrides[name] = replace(
        existing,
        print_dmin=dmin,
        print_exposure_shift=exposure_shift,
        print_contrast=contrast,
        print_paper_black=paper_black,
        print_paper_soft_clip=paper_soft_clip,
    )
    _save_positive_overrides(overrides, paths, fs)
