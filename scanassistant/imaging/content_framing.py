"""Content-frame detection: finds the actual photographed image within an
already-cropped support frame, excluding the negative's unexposed border.

Operates directly on the developed master array (`imaging.master.DevelopedMaster.
pixels`), already computed once per image regardless of which export kind is
being produced — no extra RAW decode, no extra geometry pass. The support
frame's own footprint within that array (`imaging.geometry.GeometryResult.
frame_in_output`) is always axis-aligned by construction (the deskew is
already resolved), so this needs no rotation handling of its own: a thin band
at the very edge of the support frame is assumed sure background (the
photographed content essentially never touches that edge — the film border
always leaves some room), a ring further in is probable background, and a
centered core is probable foreground. No dependency on PySide6.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from scanassistant.imaging.geometry import FrameGeometry

_WORKING_LONG_EDGE_PX = 350  # measured on real samples: NOT simply "smaller is safer
# but blurrier" — a busy, high-contrast photo (foreground clutter against a much
# darker background) made GrabCut confidently carve the image itself in half at
# 200px (fill 0.84-0.86, clears both thresholds — a real false positive, not a
# missed detection) while the *same* image correctly stayed unconfident at
# 300-600px (fill 0.75-0.83). 350px was the smallest tested resolution with a
# comfortable safety margin below `_MIN_FILL` on that case, while still recovering
# the correct crop on a separate case that needed >=350px to succeed at all —
# and about 5-10x faster than the original 600px choice on the same samples.
_OUTER_MARGIN_FRACTION = 0.04
_CORE_INSET_FRACTION = 0.85  # centered core, sized at this fraction of the support frame
_GRABCUT_ITERS = 2  # more iterations did not change the outcome on any tested sample
# at this resolution (GrabCut converges quickly once the working image is this size).
_GRABCUT_SEED = 12345  # cv2.grabCut's GMM init is otherwise unseeded (verified elsewhere
# in this package: two runs on the same image can give different rectangles without this).
_MIN_FILL = 0.80
# Calibrated against the 2024_5_1 campaign's own journal (570 "applied"
# outcomes): at 0.55, one real crop landed at 0.592 — only 0.04 above
# the cutoff, essentially a coin flip away from being (correctly) deferred
# instead of silently trusted. Raised to 0.62: closes that near-miss and
# reclassifies 8/570 (1.4%) borderline crops to `deferred`, without touching
# the bulk of the distribution (p10 was already 0.654). `_MIN_FILL` left
# alone — nothing in that same data landed within 0.06 of its cutoff, no
# real near-miss to calibrate against.
_MIN_AREA_RATIO = 0.62
_MARGIN_PCT = 2.0  # biased to slightly over-include rather than crop tight against
# the detected boundary — matches the operator's own stated preference (a sliver of
# unexposed border left in is fine, cutting into the photo itself is not).
_MAX_MARGIN_GAP_FRACTION = 0.3  # capped: a fixed percentage alone can, on a small
# real crop, exceed the actual room available and erase it back to nearly the full
# support frame (same failure already found and fixed for the support frame's own
# margin — the fix here is the same shape, applied to a different rectangle).


@dataclass(frozen=True)
class ContentFrameResult:
    """Content frame, in `master.pixels` coordinates — always axis-aligned."""

    x: int
    y: int
    width: int
    height: int
    fill: float
    area_ratio: float


def detect_content_frame(
    master_pixels: np.ndarray, support_in_output: FrameGeometry
) -> ContentFrameResult | None:
    """Detects the photographed content within `support_in_output`'s own
    bounds inside `master_pixels`. Returns `None` if no plausible content
    frame is found (nothing to crop) or the result isn't confident enough
    to apply — never a low-confidence guess."""
    array_h, array_w = master_pixels.shape[:2]
    x0 = max(0, round(support_in_output.x))
    y0 = max(0, round(support_in_output.y))
    x1 = min(array_w, round(support_in_output.x + support_in_output.width))
    y1 = min(array_h, round(support_in_output.y + support_in_output.height))
    if x1 <= x0 or y1 <= y0:
        return None

    region = _to_uint8(master_pixels[y0:y1, x0:x1])
    small, scale = _reduce(region)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY) if small.ndim == 3 else small
    small_h, small_w = gray.shape[:2]

    mask = _seed_mask(small_h, small_w)
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    cv2.setRNGSeed(_GRABCUT_SEED)
    try:
        cv2.grabCut(bgr, mask, None, bgd_model, fgd_model, _GRABCUT_ITERS, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None

    foreground = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    contours, _ = cv2.findContours(foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    contour = max(contours, key=cv2.contourArea) if contours else None
    if contour is None or cv2.contourArea(contour) <= 0:
        return None

    bx, by, bw, bh = cv2.boundingRect(contour)
    contour_area = cv2.contourArea(contour)
    rect_area = bw * bh
    fill = _safe_ratio(contour_area, rect_area)
    area_ratio = _safe_ratio(rect_area, small_w * small_h)
    if fill < _MIN_FILL or area_ratio < _MIN_AREA_RATIO:
        return None

    bx, by, bw, bh = _apply_margin(bx, by, bw, bh, small_w, small_h)

    inv_scale = 1.0 / scale if scale else 1.0
    return ContentFrameResult(
        x=x0 + round(bx * inv_scale),
        y=y0 + round(by * inv_scale),
        width=round(bw * inv_scale),
        height=round(bh * inv_scale),
        fill=fill,
        area_ratio=area_ratio,
    )


def _apply_margin(
    x: float, y: float, w: float, h: float, region_w: int, region_h: int
) -> tuple[float, float, float, float]:
    margin_x = min(w * _MARGIN_PCT / 100, (region_w - w) * _MAX_MARGIN_GAP_FRACTION) / 2
    margin_y = min(h * _MARGIN_PCT / 100, (region_h - h) * _MAX_MARGIN_GAP_FRACTION) / 2
    new_x = max(0.0, x - margin_x)
    new_y = max(0.0, y - margin_y)
    new_w = min(region_w - new_x, w + 2 * margin_x)
    new_h = min(region_h - new_y, h + 2 * margin_y)
    return new_x, new_y, new_w, new_h


def _seed_mask(height: int, width: int) -> np.ndarray:
    outer_y = max(1, round(height * _OUTER_MARGIN_FRACTION))
    outer_x = max(1, round(width * _OUTER_MARGIN_FRACTION))
    core_y = max(outer_y + 1, round(height * (1 - _CORE_INSET_FRACTION) / 2))
    core_x = max(outer_x + 1, round(width * (1 - _CORE_INSET_FRACTION) / 2))

    mask = np.full((height, width), cv2.GC_BGD, np.uint8)
    mask[outer_y : height - outer_y, outer_x : width - outer_x] = cv2.GC_PR_BGD
    mask[core_y : height - core_y, core_x : width - core_x] = cv2.GC_PR_FGD
    return mask


def _to_uint8(pixels: np.ndarray) -> np.ndarray:
    if pixels.dtype == np.uint8:
        return pixels
    return (pixels // 257).astype(np.uint8)


def _reduce(
    pixels: np.ndarray, long_edge_px: int = _WORKING_LONG_EDGE_PX
) -> tuple[np.ndarray, float]:
    long_edge = max(pixels.shape[:2])
    if long_edge <= long_edge_px or long_edge == 0:
        return pixels, 1.0
    scale = long_edge_px / long_edge
    height, width = pixels.shape[:2]
    resized = cv2.resize(
        pixels,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return float(np.clip(numerator / denominator, 0.0, 1.0))
