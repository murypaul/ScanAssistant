"""Geometry: orientation and dimensions.

Starts from the full-frame array (reference space) and the already-computed
frame with margin (`imaging.framing` or manual edit) to produce the final
derivative array. Two modes: `native` (no rescaling) and `fixed` (common
output dimensions). No dependency on PySide6.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class FrameGeometry:
    """Frame with margin, in full-resolution coordinates."""

    x: float
    y: float
    width: float
    height: float
    angle_deg: float = 0.0


@dataclass(frozen=True)
class GeometryResult:
    pixels: np.ndarray
    output_width: int
    output_height: int
    scale_factor: float  # output_width / cropped_width; > 1 = upscaled
    bounds_adjusted: bool  # fixed mode: crop was constrained by image bounds
    frame_in_output: FrameGeometry  # `frame`'s own footprint, in output-pixel
    # coordinates: fills the whole array in native mode, but in fixed mode the
    # actual crop can be extended past `frame` to match the target ratio, so
    # `frame` no longer flush with the array edges — always axis-aligned
    # (angle_deg=0.0), the deskew is already resolved by construction.


def apply_geometry(
    pixels: np.ndarray,
    frame: FrameGeometry,
    *,
    rotation_deg: int = 0,
    size_mode: str = "native",
    final_dimensions_px: tuple[int, int] = (6016, 4016),
) -> GeometryResult:
    """Deskews, crops, rotates (0/90/180/270°, V key), and (fixed mode) rescales the frame."""
    if frame.width <= 0 or frame.height <= 0:
        # No valid frame (e.g. no detection yet, framing disabled): falls
        # back to the whole image, same as the IMPOSSIBLE case — never a
        # degenerate 1x1 px frame.
        height, width = pixels.shape[:2]
        frame = FrameGeometry(x=0, y=0, width=width, height=height, angle_deg=0.0)
    if size_mode == "fixed":
        return _apply_fixed(pixels, frame, rotation_deg, final_dimensions_px)
    return _apply_native(pixels, frame, rotation_deg)


def _apply_native(pixels: np.ndarray, frame: FrameGeometry, rotation_deg: int) -> GeometryResult:
    cx = frame.x + frame.width / 2
    cy = frame.y + frame.height / 2
    cropped = _deskew_and_crop(pixels, frame, frame.width, frame.height, center=(cx, cy))
    cropped = _rotate(cropped, rotation_deg)
    height, width = cropped.shape[:2]
    return GeometryResult(
        pixels=cropped,
        output_width=width,
        output_height=height,
        scale_factor=1.0,
        bounds_adjusted=False,
        # The crop is exactly `frame`, nothing added: it fills the whole array.
        frame_in_output=FrameGeometry(x=0, y=0, width=width, height=height, angle_deg=0.0),
    )


def _apply_fixed(
    pixels: np.ndarray,
    frame: FrameGeometry,
    rotation_deg: int,
    final_dimensions_px: tuple[int, int],
) -> GeometryResult:
    image_height, image_width = pixels.shape[:2]
    target_w, target_h = final_dimensions_px
    rotated_90 = rotation_deg in (90, 270)
    if rotated_90:
        target_w, target_h = target_h, target_w
    # The crop itself is sized in *pre-rotation* space (the frame lives in the
    # original, unrotated image); a 90°/270° rotation swaps axes afterwards,
    # so the ratio used to size the crop must be the inverse of the (already
    # swapped) target ratio.
    ratio = target_h / target_w if rotated_90 else target_w / target_h

    width, height = frame.width, frame.height
    if height <= 0:
        height = 1.0
    frame_ratio = width / height
    if frame_ratio < ratio:
        width = height * ratio
    elif frame_ratio > ratio:
        height = width / ratio

    bounds_adjusted = False
    if width > image_width:
        width = float(image_width)
        height = width / ratio
        bounds_adjusted = True
    if height > image_height:
        height = float(image_height)
        width = height * ratio
        bounds_adjusted = True

    cx = frame.x + frame.width / 2
    cy = frame.y + frame.height / 2
    half_w, half_h = width / 2, height / 2
    clamped_cx = min(max(cx, half_w), image_width - half_w)
    clamped_cy = min(max(cy, half_h), image_height - half_h)
    if clamped_cx != cx or clamped_cy != cy:
        bounds_adjusted = True

    cropped = _deskew_and_crop(pixels, frame, width, height, center=(clamped_cx, clamped_cy))
    crop_w, crop_h = cropped.shape[1], cropped.shape[0]
    # `frame` itself, relative to the crop's own top-left — before ratio
    # extension, the crop may be centered off `frame`'s own center (bounds
    # clamping), so this isn't simply `(0, 0)` the way native mode's is.
    local_frame = FrameGeometry(
        x=frame.x - (clamped_cx - width / 2),
        y=frame.y - (clamped_cy - height / 2),
        width=frame.width,
        height=frame.height,
    )
    cropped = _rotate(cropped, rotation_deg)
    local_frame, (crop_w, crop_h) = _rotate_rect(local_frame, (crop_w, crop_h), rotation_deg)
    resized = cv2.resize(
        cropped, (round(target_w), round(target_h)), interpolation=cv2.INTER_LANCZOS4
    )
    scale_factor = target_w / (height if rotated_90 else width) if width else 1.0
    scale_x = round(target_w) / crop_w if crop_w else 1.0
    scale_y = round(target_h) / crop_h if crop_h else 1.0
    frame_in_output = FrameGeometry(
        x=local_frame.x * scale_x,
        y=local_frame.y * scale_y,
        width=local_frame.width * scale_x,
        height=local_frame.height * scale_y,
        angle_deg=0.0,
    )
    return GeometryResult(
        pixels=resized,
        output_width=round(target_w),
        output_height=round(target_h),
        scale_factor=scale_factor,
        bounds_adjusted=bounds_adjusted,
        frame_in_output=frame_in_output,
    )


def _deskew_and_crop(
    pixels: np.ndarray,
    frame: FrameGeometry,
    width: float,
    height: float,
    *,
    center: tuple[float, float],
) -> np.ndarray:
    cx, cy = center
    if frame.angle_deg != 0:
        image_height, image_width = pixels.shape[:2]
        matrix = cv2.getRotationMatrix2D((cx, cy), frame.angle_deg, 1.0)
        pixels = cv2.warpAffine(
            pixels,
            matrix,
            (image_width, image_height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
    return _crop_axis_aligned(pixels, cx, cy, width, height)


def _crop_axis_aligned(
    pixels: np.ndarray, cx: float, cy: float, width: float, height: float
) -> np.ndarray:
    """Axis-aligned crop centered on `(cx, cy)`, replicating edge pixels past the bounds."""
    image_height, image_width = pixels.shape[:2]
    out_w, out_h = max(1, round(width)), max(1, round(height))
    x0 = round(cx - width / 2)
    y0 = round(cy - height / 2)
    x1, y1 = x0 + out_w, y0 + out_h

    pad_left, pad_top = max(0, -x0), max(0, -y0)
    pad_right, pad_bottom = max(0, x1 - image_width), max(0, y1 - image_height)
    if pad_left or pad_top or pad_right or pad_bottom:
        pixels = cv2.copyMakeBorder(
            pixels, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_REPLICATE
        )
        x0, y0, x1, y1 = x0 + pad_left, y0 + pad_top, x1 + pad_left, y1 + pad_top

    return np.ascontiguousarray(pixels[y0:y1, x0:x1])


def _rotate(pixels: np.ndarray, rotation_deg: int) -> np.ndarray:
    """Rotates `pixels` clockwise by 0/90/180/270° (V key, `core.session.rotate_current`)."""
    times = (rotation_deg // 90) % 4
    if times == 0:
        return pixels
    return np.ascontiguousarray(np.rot90(pixels, k=-times))


def _rotate_rect(
    rect: FrameGeometry, canvas_size: tuple[float, float], rotation_deg: int
) -> tuple[FrameGeometry, tuple[float, float]]:
    """Applies the same clockwise 0/90/180/270° rotation as `_rotate` to a
    rectangle's coordinates instead of pixels — used to track where `frame`
    itself ends up after `_rotate` moves the array around it. Returns the
    rotated rectangle and the rotated canvas's own (width, height)."""
    times = (rotation_deg // 90) % 4
    x0, y0, w, h = rect.x, rect.y, rect.width, rect.height
    width, height = canvas_size
    for _ in range(times):
        x0, y0, w, h, width, height = height - (y0 + h), x0, h, w, height, width
    return FrameGeometry(x=x0, y=y0, width=w, height=h), (width, height)
