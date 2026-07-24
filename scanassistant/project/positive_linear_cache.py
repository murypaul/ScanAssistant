"""On-disk cache of the print_engine positive engine's decoded-and-cropped
linear RAW array, downsampled — regenerable, never a source of truth.

`imaging.print_engine.render_print`'s own RAW decode + geometry crop is
the dominant cost of a print_engine render (~16.7s on the reference test
machine); the calibration screen used to re-pay it once per image per
screen session (`gui.screens.positive_review._linear_cache`, in-memory
only, cleared on every restart). This module persists a downsampled copy
per image across app restarts, written by whichever pass decodes the RAW
first — the capture-time positive-finalize pass
(`core.positive_finalize_runner`, already running on 3-4 background
workers in parallel with capture) or a batch regeneration — so by the
time an operator opens the calibration screen, browsing an already-
finalized campaign is instant instead of paying that decode again for
every single image.

Downsampled (`_MAX_DIM`), not full resolution: the interactive preview
this feeds doesn't need native resolution to judge tone/crop by eye (same
reasoning the legacy engine's own preview already applies at a smaller
scale) and a full-resolution float cache would be enormous. Confirm/
regenerate/Apply-to-selection always re-decode at full resolution
regardless of what's cached here, so final export quality is never
affected by this cache — losing or discarding it is always safe, just
slower on the next read.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from scanassistant.imaging.geometry import FrameGeometry
from scanassistant.project.layout import CampaignPaths
from scanassistant.utils.atomic import atomic_write_bytes

SCHEMA_VERSION = 1
_MAX_DIM = 2200


@dataclass(frozen=True)
class DecodeFingerprint:
    """Identifies exactly which decode inputs a cache entry was built
    from — a mismatch means the cache is stale (RAW replaced, support
    frame re-detected, geometry or white balance changed since it was
    written) and must not be trusted. Includes the support frame itself
    (`frame`, pre-geometry, full-resolution coordinates) — not just the
    RAW file identity — since a re-detected/re-confirmed frame (crash
    recovery, a retry) changes what the decode actually crops to without
    changing the RAW file it reads."""

    raw_size: int
    raw_mtime_ns: int
    frame: tuple[float, float, float, float, float]
    rotation_deg: int
    size_mode: str
    final_dimensions_px: tuple[int, int]
    white_balance: tuple[float, ...] | None

    @classmethod
    def for_decode(
        cls,
        *,
        raw_path: Path,
        frame: FrameGeometry,
        rotation_deg: int,
        size_mode: str,
        final_dimensions_px: tuple[int, int],
        white_balance: list[float] | None,
    ) -> DecodeFingerprint:
        try:
            stat = raw_path.stat()
            raw_size, raw_mtime_ns = stat.st_size, stat.st_mtime_ns
        except OSError:
            # A test double's synthetic RAW path, most likely — a decode
            # that got this far already read the real thing if there was
            # one; treat it like any other fingerprint field, not a reason
            # to fail the render itself.
            raw_size, raw_mtime_ns = -1, -1
        return cls(
            raw_size=raw_size,
            raw_mtime_ns=raw_mtime_ns,
            frame=(frame.x, frame.y, frame.width, frame.height, frame.angle_deg),
            rotation_deg=rotation_deg,
            size_mode=size_mode,
            final_dimensions_px=(final_dimensions_px[0], final_dimensions_px[1]),
            white_balance=tuple(white_balance) if white_balance is not None else None,
        )

    def to_json(self) -> dict:
        return {
            "raw_size": self.raw_size,
            "raw_mtime_ns": self.raw_mtime_ns,
            "frame": list(self.frame),
            "rotation_deg": self.rotation_deg,
            "size_mode": self.size_mode,
            "final_dimensions_px": list(self.final_dimensions_px),
            "white_balance": (list(self.white_balance) if self.white_balance is not None else None),
        }

    @classmethod
    def from_json(cls, data: dict) -> DecodeFingerprint:
        wb = data.get("white_balance")
        final_dimensions = data["final_dimensions_px"]
        frame = data["frame"]
        return cls(
            raw_size=data["raw_size"],
            raw_mtime_ns=data["raw_mtime_ns"],
            frame=(frame[0], frame[1], frame[2], frame[3], frame[4]),
            rotation_deg=data["rotation_deg"],
            size_mode=data["size_mode"],
            final_dimensions_px=(final_dimensions[0], final_dimensions[1]),
            white_balance=tuple(wb) if wb is not None else None,
        )


def _paths(paths: CampaignPaths, name: str) -> tuple[Path, Path]:
    directory = paths.print_cache_dir
    return directory / f"{name}.npy", directory / f"{name}.json"


def save(
    paths: CampaignPaths,
    name: str,
    linear: np.ndarray,
    frame_in_output: FrameGeometry,
    fingerprint: DecodeFingerprint,
) -> None:
    """Downsamples and persists `linear`/`frame_in_output`, atomically.

    Never raises on a write failure (disk full, permission, a removable
    campaign volume gone read-only): this cache is a pure optimization,
    losing an entry just means the next reader re-decodes, same as a cold
    cache — it must never be the thing that turns a slow render into a
    hard failure."""
    try:
        directory = paths.print_cache_dir
        directory.mkdir(exist_ok=True)
        height, width = linear.shape[:2]
        scale = min(1.0, _MAX_DIM / max(height, width))
        source = linear.astype(np.float32)
        if scale < 1.0:
            small = cv2.resize(
                source,
                (round(width * scale), round(height * scale)),
                interpolation=cv2.INTER_AREA,
            )
            scaled_frame = FrameGeometry(
                x=frame_in_output.x * scale,
                y=frame_in_output.y * scale,
                width=frame_in_output.width * scale,
                height=frame_in_output.height * scale,
                angle_deg=frame_in_output.angle_deg,
            )
        else:
            small, scaled_frame = source, frame_in_output

        array_path, meta_path = _paths(paths, name)
        buf = io.BytesIO()
        np.save(buf, small.astype(np.float16))
        atomic_write_bytes(array_path, buf.getvalue())
        meta = {
            "schema": SCHEMA_VERSION,
            "frame": {
                "x": scaled_frame.x,
                "y": scaled_frame.y,
                "width": scaled_frame.width,
                "height": scaled_frame.height,
                "angle_deg": scaled_frame.angle_deg,
            },
            "fingerprint": fingerprint.to_json(),
        }
        atomic_write_bytes(meta_path, json.dumps(meta).encode("utf-8"))
    except OSError:
        pass


def load(
    paths: CampaignPaths, name: str, fingerprint: DecodeFingerprint
) -> tuple[np.ndarray, FrameGeometry] | None:
    """`None` if there's no cache entry, it doesn't match `fingerprint`
    (stale), or it can't be read (corrupt, a partial write from a killed
    process) — never raises either way, same "worst case is a cold cache"
    contract as `save`."""
    array_path, meta_path = _paths(paths, name)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema") != SCHEMA_VERSION:
            return None
        if DecodeFingerprint.from_json(meta["fingerprint"]) != fingerprint:
            return None
        linear = np.load(array_path).astype(np.float32)
        frame = FrameGeometry(**meta["frame"])
        return linear, frame
    except (OSError, ValueError, KeyError, TypeError):
        return None
