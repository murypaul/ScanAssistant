"""Automatic frame detection and confidence scoring.

Algorithm: downscale → grayscale + blur → inverted Otsu threshold →
morphological closing → largest external contour → `minAreaRect` →
margin → bounded deskew. The confidence score measures the intrinsic
geometric quality of the **detected** rectangle (before margin): no
dependency on a target ratio or size. No dependency on PySide6.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

REDUCED_LONG_EDGE_PX = 1600
_NEAR_EDGE_FRACTION_PCT = 1.0  # 1% of the long edge (c_rect, c_border)
_MORPH_KERNEL_FRACTION = 0.02  # ~2% of the long edge (morphological closing)

RELIABLE = "reliable"
REVIEW = "review"
IMPOSSIBLE = "impossible"


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
    reliable_threshold: float = 0.90,
    review_threshold: float = 0.60,
    threshold_bias: int = 0,
) -> FrameResult:
    """Detects the largest dark rectangle (negative) on a light background."""
    height, width = pixels.shape[:2]

    small, scale = _reduce(pixels)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY) if small.ndim == 3 else small
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    binary = _threshold_otsu_inverted(blurred, threshold_bias)
    closed = _close(binary)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    contour = max(contours, key=cv2.contourArea) if contours else None
    if contour is None or cv2.contourArea(contour) <= 0:
        return FrameResult(
            x=0,
            y=0,
            width=width,
            height=height,
            angle_deg=0.0,
            confidence=0.0,
            level=IMPOSSIBLE,
            components=_ZERO_COMPONENTS,
        )

    (center_x, center_y), (rect_w, rect_h), raw_angle = cv2.minAreaRect(contour)
    rect_w, rect_h, angle = _normalize_angle(rect_w, rect_h, raw_angle)

    small_long_edge = max(small.shape[:2])
    components = _confidence_components(
        contour, (center_x, center_y, rect_w, rect_h, angle), small.shape[:2], small_long_edge
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


def _reduce(pixels: np.ndarray) -> tuple[np.ndarray, float]:
    long_edge = max(pixels.shape[:2])
    if long_edge <= REDUCED_LONG_EDGE_PX or long_edge == 0:
        return pixels, 1.0
    scale = REDUCED_LONG_EDGE_PX / long_edge
    height, width = pixels.shape[:2]
    resized = cv2.resize(
        pixels,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def _threshold_otsu_inverted(blurred: np.ndarray, threshold_bias: int) -> np.ndarray:
    otsu_value, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if threshold_bias == 0:
        _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        return binary
    biased = float(np.clip(otsu_value + threshold_bias, 0, 255))
    _, binary = cv2.threshold(blurred, biased, 255, cv2.THRESH_BINARY_INV)
    return binary


def _close(binary: np.ndarray) -> np.ndarray:
    long_edge = max(binary.shape[:2])
    kernel_size = _odd(max(3, round(long_edge * _MORPH_KERNEL_FRACTION)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)


def _odd(n: int) -> int:
    return n if n % 2 == 1 else n + 1


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
