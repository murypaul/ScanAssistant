"""Automatic frame detection and confidence scoring.

Algorithm: downscale → mask-seeded GrabCut (outer margin assumed
background, center anchor assumed foreground) → largest external contour
→ `minAreaRect` → margin → bounded deskew. The confidence score measures
the intrinsic geometric quality of the **detected** rectangle (before
margin): no dependency on a target ratio or size. No dependency on
PySide6.

**Not Otsu-threshold-based** (DECISIONS.md I-185, revises the original
design): a single global brightness split can't tell the negative's own
light-toned unexposed border apart from the light table behind it, so it
systematically excludes that border from the detected rectangle — measured
on a real campaign (`2024_5_1`) to affect the *entire* "reliable" bucket
(100% of 14 high-confidence detections were later manually corrected,
always larger than auto in both dimensions). GrabCut's colour-distribution
model, already proven for the same "exclude the border" problem in
`imaging.content_framing`, doesn't share that specific blind spot —
re-measured on the same campaign's 120 real auto/manual pairs: the
"reliable" bucket now covers 98% of images (vs. 15% before) at a *better*
median accuracy (IoU 0.93 vs 0.89).

**The confidence score's correlation with actual correctness is weak for
either algorithm** (measured ≈0 for GrabCut on real data) — it measures
how clean/self-consistent the *found* rectangle is, not whether it's the
*right* one, and a smoothly-filled but wrong GrabCut mask can still score
near the ceiling (a real counterexample: confidence 0.998, IoU 0.21 against
the manual correction, on a negative whose own content has a much lighter
region abutting a much darker one — both algorithms independently
undershoot into the lighter region only). Cross-checking GrabCut's result
against an independent Otsu-based detection was tried and measured *not*
to help (94% agreement between the two, and ≈0 correlation between that
agreement and actual accuracy, on the same real sample) — both methods
share this specific blind spot rather than failing independently, so
agreement between them isn't informative. Documented as a known,
unresolved limitation rather than papered over; see DECISIONS.md I-185.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

REDUCED_LONG_EDGE_PX = 1300  # `_DEFAULT_WORKING_LONG_EDGE_PX` — see `BUDGET_TIERS_S`
_NEAR_EDGE_FRACTION_PCT = 1.0  # 1% of the long edge (c_rect, c_border)

RELIABLE = "reliable"
REVIEW = "review"
IMPOSSIBLE = "impossible"

# GrabCut seed geometry (mask-initialized: outer margin = sure background,
# a small center anchor = sure foreground, everything else "probably
# foreground" for GrabCut's own model to refine) — an operator never places
# the negative touching the frame edge exactly, and the seed only needs to
# give GrabCut's foreground/background colour models a first sample each to
# build from, not a precise starting boundary.
_OUTER_MARGIN_FRACTION = 0.04
_CENTER_ANCHOR_FRACTION = 0.10
_GRABCUT_SEED = 12345  # cv2.grabCut's GMM init is otherwise unseeded — verified
# non-deterministic without this (two runs on the same image gave different
# rectangles).
_DEFAULT_GRABCUT_ITERS = 8
# Grey-level standard deviation, on the downscaled working image, below
# which there's nothing for GrabCut to segment (see `detect_frame`'s own
# comment at the call site for the measured cost of *not* short-circuiting
# this case). Real capture noise alone measures well above this on any
# genuine negative, even a severely underexposed one (a real low-contrast
# but real synthetic test case measured ~2.3 vs. a perfectly flat one at
# 0.0) — conservative enough not to misfire on subtle-but-real content.
_MIN_STD_FOR_SEGMENTATION = 1.0

# Capture-time processing-budget tiers (06_INTERFACE.md / DECISIONS.md
# I-185): (working_long_edge_px, grabcut_iters) pairs, empirically timed and
# accuracy-checked against 120 real auto/manual pairs from `2024_5_1`
# (`working_long_edge_px=1300, grabcut_iters=8` — the tier below labeled
# "4s" — is the one actually validated end-to-end; the others are the same
# GrabCut algorithm at a different resolution/iteration cost, timed but not
# separately accuracy-validated). Deliberately not the operator-proposed
# 2/3/4/5/7/10/12/15/20s ladder: measurement showed no distinct, meaningfully
# different configuration for a "12s" tier (falls between the 1600px/12-iter
# and 2000px/8-iter points already covered) or a "20s" one (max measured
# ~14.5s at the highest resolution/iteration count tried, and accuracy
# plateaued between 8 and 12 iterations in spot checks well before that) —
# offering tiers that don't correspond to a real quality step would be
# cosmetic, not a real choice.
BUDGET_TIERS_S: dict[float, tuple[int, int]] = {
    2.0: (1000, 5),
    3.0: (1300, 3),
    4.0: (1300, 8),  # default — the only tier validated on all 120 real pairs
    5.0: (1600, 5),
    7.0: (1600, 8),
    10.0: (2000, 8),
    15.0: (2000, 12),
}
DEFAULT_BUDGET_S = 4.0


def budget_to_params(budget_s: float) -> tuple[int, int]:
    """Nearest defined tier at or below `budget_s` (never silently rounds
    *up* past what the operator asked to spend) — falls back to the
    cheapest tier if `budget_s` is below all of them, so an invalid/stale
    config value degrades to "fast" rather than raising."""
    eligible = [tier for tier in BUDGET_TIERS_S if tier <= budget_s]
    chosen = max(eligible) if eligible else min(BUDGET_TIERS_S)
    return BUDGET_TIERS_S[chosen]


@dataclass(frozen=True)
class ConfidenceComponents:
    c_fill: float
    c_rect: float
    c_size: float
    c_border: float
    c_solidity: float

    @property
    def confidence(self) -> float:
        return (
            self.c_fill**0.30
            * self.c_rect**0.30
            * self.c_size**0.20
            * self.c_border**0.10
            * self.c_solidity**0.10
        )


_ZERO_COMPONENTS = ConfidenceComponents(
    c_fill=0.0, c_rect=0.0, c_size=0.0, c_border=0.0, c_solidity=0.0
)


@dataclass(frozen=True)
class FrameResult:
    """Detected frame, in full-resolution coordinates (reference space)."""

    x: int
    y: int
    width: int
    height: int
    angle_deg: float
    confidence: float
    level: str  # reliable | review | impossible
    components: ConfidenceComponents
    deskew_clamped: bool = False


def classify(confidence: float, *, reliable_threshold: float, review_threshold: float) -> str:
    if confidence >= reliable_threshold:
        return RELIABLE
    if confidence >= review_threshold:
        return REVIEW
    return IMPOSSIBLE


def detect_frame(
    pixels: np.ndarray,
    *,
    margin_pct: float = 2.0,
    max_deskew_deg: float = 5.0,
    reliable_threshold: float = 0.93,
    review_threshold: float = 0.85,
    working_long_edge_px: int = REDUCED_LONG_EDGE_PX,
    grabcut_iters: int = _DEFAULT_GRABCUT_ITERS,
) -> FrameResult:
    """Detects the negative (support frame) on a light background via a
    mask-seeded GrabCut segmentation (module docstring) — `working_long_edge_px`/
    `grabcut_iters` set the quality/time tradeoff, normally chosen through
    `budget_to_params` from a campaign's `framing.detection_budget_s`
    rather than passed directly."""
    height, width = pixels.shape[:2]

    small, scale = _reduce(pixels, working_long_edge_px)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY) if small.ndim == 3 else small
    if float(np.std(gray)) < _MIN_STD_FOR_SEGMENTATION:
        # A genuinely near-uniform image (nothing there to segment — an
        # accidental blank-table capture, or a synthetic test fixture with
        # no structure at all) is a pathological case for GrabCut's
        # iterative colour-model refinement: measured 7-28s (vs. 1-3s on
        # real/structured content at the same resolution/iteration count)
        # trying to converge on a foreground/background split that doesn't
        # exist, for the same IMPOSSIBLE result it would eventually reach
        # anyway. Skipping straight there keeps worst-case latency bounded
        # without changing the outcome.
        return _impossible_result(width, height)
    contour = _grabcut_contour_from_gray(gray, grabcut_iters)
    if contour is None or cv2.contourArea(contour) <= 0:
        return _impossible_result(width, height)

    return _frame_from_contour(
        contour,
        small.shape[:2],
        scale,
        width,
        height,
        margin_pct=margin_pct,
        max_deskew_deg=max_deskew_deg,
        reliable_threshold=reliable_threshold,
        review_threshold=review_threshold,
    )


def _grabcut_contour_from_gray(gray: np.ndarray, grabcut_iters: int) -> np.ndarray | None:
    height, width = gray.shape[:2]

    margin_y = max(1, round(height * _OUTER_MARGIN_FRACTION))
    margin_x = max(1, round(width * _OUTER_MARGIN_FRACTION))
    mask = np.full((height, width), cv2.GC_PR_FGD, np.uint8)
    mask[:margin_y, :] = cv2.GC_BGD
    mask[height - margin_y :, :] = cv2.GC_BGD
    mask[:, :margin_x] = cv2.GC_BGD
    mask[:, width - margin_x :] = cv2.GC_BGD
    center_y, center_x = height // 2, width // 2
    anchor_h = max(1, round(height * _CENTER_ANCHOR_FRACTION))
    anchor_w = max(1, round(width * _CENTER_ANCHOR_FRACTION))
    mask[center_y - anchor_h : center_y + anchor_h, center_x - anchor_w : center_x + anchor_w] = (
        cv2.GC_FGD
    )

    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    cv2.setRNGSeed(_GRABCUT_SEED)
    try:
        cv2.grabCut(bgr, mask, None, bgd_model, fgd_model, grabcut_iters, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None

    foreground = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    contours, _ = cv2.findContours(foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    return max(contours, key=cv2.contourArea) if contours else None


def _impossible_result(width: int, height: int, confidence: float = 0.0) -> FrameResult:
    return FrameResult(
        x=0,
        y=0,
        width=width,
        height=height,
        angle_deg=0.0,
        confidence=confidence,
        level=IMPOSSIBLE,
        components=_ZERO_COMPONENTS,
    )


def _frame_from_contour(
    contour: np.ndarray,
    small_shape: tuple[int, int],
    scale: float,
    width: int,
    height: int,
    *,
    margin_pct: float,
    max_deskew_deg: float,
    reliable_threshold: float,
    review_threshold: float,
) -> FrameResult:
    """Turns a contour (found by `_grabcut_contour`) into a scored, margined
    `FrameResult` in full-resolution coordinates."""
    (center_x, center_y), (rect_w, rect_h), raw_angle = cv2.minAreaRect(contour)
    rect_w, rect_h, angle = _normalize_angle(rect_w, rect_h, raw_angle)

    small_long_edge = max(small_shape)
    components = _confidence_components(
        contour, (center_x, center_y, rect_w, rect_h, angle), small_shape, small_long_edge
    )
    confidence = components.confidence
    level = classify(
        confidence, reliable_threshold=reliable_threshold, review_threshold=review_threshold
    )

    if level == IMPOSSIBLE:
        return FrameResult(
            x=0,
            y=0,
            width=width,
            height=height,
            angle_deg=0.0,
            confidence=confidence,
            level=IMPOSSIBLE,
            components=components,
        )

    margin_factor = 1 + margin_pct / 100
    rect_w *= margin_factor
    rect_h *= margin_factor

    deskew_clamped = abs(angle) > max_deskew_deg
    effective_angle = 0.0 if deskew_clamped else angle

    inv_scale = 1.0 / scale if scale else 1.0
    full_w = rect_w * inv_scale
    full_h = rect_h * inv_scale
    full_x = center_x * inv_scale - full_w / 2
    full_y = center_y * inv_scale - full_h / 2

    return FrameResult(
        x=round(full_x),
        y=round(full_y),
        width=round(full_w),
        height=round(full_h),
        angle_deg=effective_angle,
        confidence=confidence,
        level=level,
        components=components,
        deskew_clamped=deskew_clamped,
    )


# --- algorithm steps --------------------------------------------------------


def _reduce(
    pixels: np.ndarray, long_edge_px: int = REDUCED_LONG_EDGE_PX
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


def _normalize_angle(w: float, h: float, angle: float) -> tuple[float, float, float]:
    """Brings the `cv2.minAreaRect` angle into [-45°;45°] (normalized convention)."""
    if angle > 45:
        w, h = h, w
        angle -= 90
    elif angle < -45:
        w, h = h, w
        angle += 90
    return w, h, angle


# --- confidence score --------------------------------------------------------


def _confidence_components(
    contour: np.ndarray,
    rect: tuple[float, float, float, float, float],
    image_shape: tuple[int, int],
    long_edge: float,
) -> ConfidenceComponents:
    center_x, center_y, rect_w, rect_h, angle = rect
    box_points = cv2.boxPoints(((center_x, center_y), (rect_w, rect_h), angle))

    contour_area = cv2.contourArea(contour)
    rect_area = rect_w * rect_h
    hull = cv2.convexHull(contour)
    hull_area = cv2.contourArea(hull)

    c_fill = _safe_ratio(contour_area, rect_area)
    c_solidity = _safe_ratio(contour_area, hull_area)
    c_size = _c_size(rect_area, image_shape[0] * image_shape[1])
    c_border = _c_border(box_points, image_shape, long_edge)
    c_rect = _c_rect(contour, box_points, long_edge)

    return ConfidenceComponents(
        c_fill=c_fill, c_rect=c_rect, c_size=c_size, c_border=c_border, c_solidity=c_solidity
    )


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return float(np.clip(numerator / denominator, 0.0, 1.0))


def _c_size(rect_area: float, image_area: float) -> float:
    if image_area <= 0:
        return 0.0
    s = rect_area / image_area
    if 0.08 <= s <= 0.92:
        return 1.0
    if s < 0.08:
        if s <= 0.02:
            return 0.0
        return (s - 0.02) / (0.08 - 0.02)
    if s >= 0.98:
        return 0.0
    return (0.98 - s) / (0.98 - 0.92)


def _c_border(box_points: np.ndarray, image_shape: tuple[int, int], long_edge: float) -> float:
    if long_edge <= 0:
        return 0.0
    image_h, image_w = image_shape
    xs = box_points[:, 0]
    ys = box_points[:, 1]
    distances = np.concatenate([xs, image_w - xs, ys, image_h - ys])
    min_distance = float(np.min(distances))
    d_pct = (min_distance / long_edge) * 100
    return float(np.clip(d_pct / _NEAR_EDGE_FRACTION_PCT, 0.0, 1.0))


def _c_rect(contour: np.ndarray, box_points: np.ndarray, long_edge: float) -> float:
    if long_edge <= 0:
        return 0.0
    points = contour.reshape(-1, 2).astype(np.float64)
    if len(points) == 0:
        return 0.0
    threshold = (_NEAR_EDGE_FRACTION_PCT / 100) * long_edge

    min_distances = np.full(len(points), np.inf)
    for i in range(4):
        p1 = box_points[i]
        p2 = box_points[(i + 1) % 4]
        distances = _point_to_segment_distance(points, p1, p2)
        min_distances = np.minimum(min_distances, distances)

    near = min_distances <= threshold
    return float(np.count_nonzero(near) / len(points))


def _point_to_segment_distance(points: np.ndarray, p1: np.ndarray, p2: np.ndarray) -> np.ndarray:
    p1 = np.asarray(p1, dtype=np.float64)
    p2 = np.asarray(p2, dtype=np.float64)
    segment = p2 - p1
    segment_len_sq = float(np.dot(segment, segment))
    if segment_len_sq == 0:
        return np.linalg.norm(points - p1, axis=1)
    t = np.clip(((points - p1) @ segment) / segment_len_sq, 0.0, 1.0)
    projection = p1 + t[:, None] * segment
    return np.linalg.norm(points - projection, axis=1)
