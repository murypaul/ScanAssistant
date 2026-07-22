"""Reading-positive pipeline, second generation: reconstructs the darkroom
print process (density relative to the film's own base, a film response
curve, then a paper response) instead of stretching an already inverted,
gamma-encoded array (`imaging.positive`).

Starts from a *linear* RAW development (`imaging.raw.RawDecoder.develop(...,
linear=True)`), then the same geometry crop the master pipeline uses
(`imaging.geometry.apply_geometry`) — this module never sees the unexposed
light table outside the support frame, only what geometry already cropped
to. Reuses the existing content-frame detector (`imaging.content_framing`,
GrabCut) for the region density statistics are drawn from, rather than a
new detector. No dependency on PySide6.

`render_print_from_linear` is the core, directly testable on synthetic
arrays; `render_print` is the thin RAW/geometry orchestration around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from scanassistant.imaging.content_framing import detect_content_frame
from scanassistant.imaging.geometry import FrameGeometry, apply_geometry
from scanassistant.imaging.raw import RawDecoder

_REC709 = np.array([0.2126, 0.7152, 0.0722])
_THRESHOLD = 1.0 / 65535.0  # guards log(0) on a pixel at or above Dmin

# Film response (fixed — a property of the film stock, not recomputed per
# image): toe/shoulder softplus compression on normalized density.
_FILM_TOE_STRENGTH = 6.0
_FILM_SHOULDER_STRENGTH = 6.0

# Paper response: contrast and exposure are computed separately (bundling
# them into one pivot/stretch was found, empirically, to misdiagnose which
# correction a given negative actually needs — DECISIONS.md I-178).
_CONTRAST_TARGET_SPREAD = 0.75
_CONTRAST_CAP_LOW = 0.6
_CONTRAST_CAP_HIGH = 2.2
_TARGET_MEAN = 0.45  # same target `imaging.positive._auto` uses
_EXPOSURE_SHIFT_FLAG = 0.20
_PAPER_BLACK = 0.02
_PAPER_SOFT_CLIP = 0.85

# Dmin ring / content-mask insets, fractions of the support frame's own
# width/height.
_BORDER_INSET_FRACTION = 0.015
_BORDER_RING_WIDTH_FRACTION = 0.07
_CONTENT_INSET_FRACTION = 0.15
_DENSITY_PERCENTILE_HIGH = 99.5

# Local contrast (bilateral filter — not CLAHE, which was found to force
# contrast into flat/uniform regions instead of leaving them alone,
# I-178): only ever applied to images the confidence signal already
# flagged, never as a default step.
_BILATERAL_SIGMA_COLOR = 0.12
_BILATERAL_SIGMA_SPACE = 15.0
_BILATERAL_DETAIL_BOOST = 2.2


@dataclass(frozen=True)
class PrintResult:
    """16-bit monochrome positive, plus the diagnostics the calibration
    screen and the journal need — never silently dropped (I-176)."""

    pixels: np.ndarray  # (H, W) uint16, monochrome, already cropped to `content_frame`
    dmin: tuple[float, float, float]
    dmax: float
    contrast: float
    raw_contrast: float
    exposure_shift: float
    flagged: bool
    content_mask_source: str  # "grabcut" | "inset_fallback"
    local_contrast_applied: bool
    content_frame: tuple[int, int, int, int]  # x, y, w, h in the geometry
    # (post support-frame-crop) coordinate space `render_print_from_linear`
    # received — same space `imaging.content_framing.ContentFrameResult`
    # already uses elsewhere, so callers can build a `ContentFrameOutcome`
    # from it exactly like the existing `imaging.positive` pipeline does.


def render_print(
    decoder: RawDecoder,
    raw_path: Path,
    frame: FrameGeometry,
    *,
    rotation_deg: int = 0,
    size_mode: str = "native",
    final_dimensions_px: tuple[int, int] = (6016, 4016),
    user_wb: list[float] | None = None,
    dmin_override: tuple[float, float, float] | None = None,
) -> PrintResult:
    """RAW linear development + geometry, then `render_print_from_linear`."""
    development = decoder.develop(raw_path, user_wb=user_wb, linear=True)
    geometry = apply_geometry(
        development.pixels,
        frame,
        rotation_deg=rotation_deg,
        size_mode=size_mode,
        final_dimensions_px=final_dimensions_px,
    )
    linear = geometry.pixels.astype(np.float64) / 65535.0
    return render_print_from_linear(linear, geometry.frame_in_output, dmin_override=dmin_override)


def render_print_from_linear(
    linear: np.ndarray,
    frame_in_output: FrameGeometry,
    *,
    dmin_override: tuple[float, float, float] | None = None,
) -> PrintResult:
    """Core algorithm (13_INVERSION_NEGATIFS.md §3-§8) on an already linear,
    already geometry-cropped array — `linear` is RGB, float64, [0, 1]."""
    dmin = (
        np.array(dmin_override, dtype=np.float64)
        if dmin_override is not None
        else _sample_dmin(linear, frame_in_output)
    )
    mask, mask_source, content_frame = _content_mask(linear, frame_in_output, dmin)

    density = np.log10(dmin[None, None, :] / np.maximum(linear, _THRESHOLD))
    density = np.clip(density, 0.0, None)
    dmax = max(float(np.percentile(density[mask], _DENSITY_PERCENTILE_HIGH)), 1e-3)
    d_norm = np.clip(density / dmax, 0.0, 1.0)

    v = _film_curve(d_norm)

    v_content = v[mask]
    mean_before = float(np.mean(v_content))
    low, high = np.percentile(v_content, [5.0, 95.0])
    spread = max(high - low, 1e-3)
    raw_contrast = _CONTRAST_TARGET_SPREAD / spread
    contrast = float(np.clip(raw_contrast, _CONTRAST_CAP_LOW, _CONTRAST_CAP_HIGH))
    flagged = not (_CONTRAST_CAP_LOW <= raw_contrast <= _CONTRAST_CAP_HIGH)
    v = np.clip(mean_before + (v - mean_before) * contrast, 0.0, 1.0)

    mean_after_contrast = float(np.mean(v[mask]))
    exposure_shift = _TARGET_MEAN - mean_after_contrast
    v = np.clip(v + exposure_shift, 0.0, 1.0)
    flagged = flagged or abs(exposure_shift) > _EXPOSURE_SHIFT_FLAG

    luminance = v @ _REC709

    local_contrast_applied = False
    if flagged:
        luminance = _local_contrast_bilateral(luminance)
        local_contrast_applied = True

    luminance = np.clip(luminance + _PAPER_BLACK, 0.0, None)
    clipped = -(luminance - _PAPER_SOFT_CLIP) / max(1e-6, 1.0 - _PAPER_SOFT_CLIP)
    above = luminance > _PAPER_SOFT_CLIP
    luminance = np.where(
        above,
        _PAPER_SOFT_CLIP + (1 - np.exp(clipped)) * (1 - _PAPER_SOFT_CLIP),
        luminance,
    )
    luminance = np.clip(luminance, 0.0, 1.0)

    pixels16 = np.clip(np.round(luminance * 65535.0), 0, 65535).astype(np.uint16)
    cx, cy, cw, ch = content_frame
    pixels16 = pixels16[cy : cy + ch, cx : cx + cw]

    return PrintResult(
        pixels=pixels16,
        dmin=(float(dmin[0]), float(dmin[1]), float(dmin[2])),
        dmax=dmax,
        contrast=contrast,
        raw_contrast=float(raw_contrast),
        exposure_shift=float(exposure_shift),
        flagged=flagged,
        content_mask_source=mask_source,
        local_contrast_applied=local_contrast_applied,
        content_frame=content_frame,
    )


def _softplus(x: np.ndarray, k: float) -> np.ndarray:
    return np.log1p(np.exp(np.clip(k * x, -50, 50))) / k


def _film_curve(d_norm: np.ndarray) -> np.ndarray:
    """Toe/shoulder compression on normalized density [0, 1] — a fixed
    shape, not recomputed per image (§5: a property of the film stock)."""
    toe = _softplus(d_norm, _FILM_TOE_STRENGTH) / _softplus(1.0, _FILM_TOE_STRENGTH)
    shoulder = 1.0 - _softplus(1.0 - toe, _FILM_SHOULDER_STRENGTH) / _softplus(
        1.0, _FILM_SHOULDER_STRENGTH
    )
    return np.clip(shoulder, 0.0, 1.0)


def _rect_mask(shape: tuple[int, int], x: int, y: int, w: int, h: int) -> np.ndarray:
    height, width = shape
    mask = np.zeros((height, width), dtype=bool)
    mask[max(0, y) : min(height, y + h), max(0, x) : min(width, x + w)] = True
    return mask


def _ring_mask(shape: tuple[int, int], x: int, y: int, w: int, h: int) -> np.ndarray:
    inset_x, inset_y = round(w * _BORDER_INSET_FRACTION), round(h * _BORDER_INSET_FRACTION)
    ring_x, ring_y = round(w * _BORDER_RING_WIDTH_FRACTION), round(h * _BORDER_RING_WIDTH_FRACTION)
    outer = _rect_mask(shape, x + inset_x, y + inset_y, w - 2 * inset_x, h - 2 * inset_y)
    inner = _rect_mask(
        shape,
        x + inset_x + ring_x,
        y + inset_y + ring_y,
        w - 2 * (inset_x + ring_x),
        h - 2 * (inset_y + ring_y),
    )
    return outer & ~inner


def _sample_dmin(linear: np.ndarray, frame: FrameGeometry) -> np.ndarray:
    """Robust (90th percentile, not mean/max) per-channel sample of the
    unexposed border just inside the support frame — a dust speck or
    scratch in the ring shouldn't pull the whole estimate off (§4)."""
    x, y, w, h = round(frame.x), round(frame.y), round(frame.width), round(frame.height)
    mask = _ring_mask(linear.shape[:2], x, y, w, h)
    if mask.sum() < 100:
        mask = _rect_mask(linear.shape[:2], x, y, w, h)
    return np.percentile(linear[mask], 90, axis=0)


def _content_mask(
    linear: np.ndarray, frame: FrameGeometry, dmin: np.ndarray
) -> tuple[np.ndarray, str, tuple[int, int, int, int]]:
    """Content region for the density statistics *and* the final crop —
    GrabCut (`imaging.content_framing`, same detector already in
    production) when confident, else a rectangular inset. Either way, a
    second pass removes any pixel whose density clips to 0 on every
    channel from the *statistics* mask only (never the crop rectangle,
    which stays a clean rectangle): brighter than the measured Dmin
    everywhere, so by definition not exposed content — found on a real
    sample where the inset fallback let 30% of a support frame's own
    unexposed border leak into the "content" statistics (I-177)."""
    preview8 = np.clip(np.sqrt(np.clip(linear, 0, 1)) * 255, 0, 255).astype(np.uint8)
    result = detect_content_frame(preview8, frame)
    shape = linear.shape[:2]
    source: str | None = None
    rect: tuple[int, int, int, int] | None = None
    if result is not None:
        candidate = _rect_mask(shape, result.x, result.y, result.width, result.height)
        if candidate.sum() >= 1000:
            mask, source = candidate, "grabcut"
            rect = (result.x, result.y, result.width, result.height)
    if source is None:
        x = round(frame.x + frame.width * _CONTENT_INSET_FRACTION)
        y = round(frame.y + frame.height * _CONTENT_INSET_FRACTION)
        w = round(frame.width * (1 - 2 * _CONTENT_INSET_FRACTION))
        h = round(frame.height * (1 - 2 * _CONTENT_INSET_FRACTION))
        mask = _rect_mask(shape, x, y, w, h)
        source = "inset_fallback"
        rect = (x, y, w, h)

    density = np.log10(dmin[None, None, :] / np.maximum(linear, _THRESHOLD))
    not_border = (density > 0.0).any(axis=-1)
    refined = mask & not_border
    # refinement too aggressive on a degenerate case: keep the coarse mask
    stats_mask = refined if refined.sum() >= 1000 else mask
    return stats_mask, source, rect


def _local_contrast_bilateral(luminance: np.ndarray) -> np.ndarray:
    """Edge-preserving local contrast boost, flagged images only — leaves
    near-uniform regions alone by construction (unlike CLAHE, which forces
    contrast into a flat tile regardless of whether there's real detail to
    recover there, measured to turn a genuinely saturated black region
    into flat mid-grey — I-178)."""
    luminance32 = luminance.astype(np.float32)
    smooth = cv2.bilateralFilter(
        luminance32, d=0, sigmaColor=_BILATERAL_SIGMA_COLOR, sigmaSpace=_BILATERAL_SIGMA_SPACE
    )
    detail = luminance32 - smooth
    boosted = smooth + detail * _BILATERAL_DETAIL_BOOST
    return np.clip(boosted, 0.0, 1.0).astype(np.float64)
