"""Preview extraction and normalization.

Preview chain (real-time): embedded RAW thumbnail → decode →
orientation normalization (EXIF flag) → displayable RGB8 array. Never a
full development here. This module never imports PySide6: conversion to
a displayable Qt type lives in `gui`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from scanassistant.imaging.raw import RawDecoder, RawThumbnail

MIN_PREVIEW_LONG_EDGE_PX = 1024

# LibRaw flip codes (`sizes.flip`) -> PIL transpose.
# Best-effort mapping: flip=0 confirmed on real RAW files, flip=3/5/6
# unverified for lack of a rotated sample.
_FLIP_TO_PIL_TRANSPOSE: dict[int, Image.Transpose] = {
    3: Image.Transpose.ROTATE_180,
    5: Image.Transpose.ROTATE_90,
    6: Image.Transpose.ROTATE_270,
}


@dataclass(frozen=True)
class Preview:
    """Normalized preview, ready to display."""

    pixels: np.ndarray  # (H, W, 3) uint8 RGB, reference space
    width: int
    height: int
    scale_factor: float  # reference_width / preview_width


def extract_preview(
    path: Path, decoder: RawDecoder, *, user_wb: list[float] | None = None
) -> Preview:
    """Embedded thumbnail, falling back to a full-frame preview if missing/too
    small — or, whenever the session has a fixed white balance, skipped
    outright in favor of the full-frame preview: the embedded thumbnail is
    the camera's own JPEG, already rendered with the camera's per-shot
    white balance, and can't be recolored after the fact."""
    thumb = None if user_wb is not None else decoder.read_thumbnail(path)
    if thumb is None or not _is_usable(thumb):
        thumb = decoder.read_full_preview(path, user_wb=user_wb)

    image = _decode(thumb)
    image = _apply_orientation(image, thumb.flip)
    pixels = np.asarray(image, dtype=np.uint8)

    reference_width = thumb.reference_width or pixels.shape[1]
    scale_factor = reference_width / pixels.shape[1] if pixels.shape[1] else 1.0

    return Preview(
        pixels=pixels, width=pixels.shape[1], height=pixels.shape[0], scale_factor=scale_factor
    )


def _is_usable(thumb: RawThumbnail) -> bool:
    if thumb.format == "none":
        return False
    long_edge = max(_thumb_size(thumb))
    return long_edge >= MIN_PREVIEW_LONG_EDGE_PX


def _thumb_size(thumb: RawThumbnail) -> tuple[int, int]:
    if thumb.format == "jpeg":
        with Image.open(io.BytesIO(thumb.data)) as image:
            return image.size  # (width, height)
    return (thumb.width, thumb.height)


def _decode(thumb: RawThumbnail) -> Image.Image:
    if thumb.format == "jpeg":
        return Image.open(io.BytesIO(thumb.data)).convert("RGB")
    array = np.frombuffer(thumb.data, dtype=np.uint8).reshape(thumb.height, thumb.width, 3)
    return Image.fromarray(array, mode="RGB")


def _apply_orientation(image: Image.Image, flip: int) -> Image.Image:
    transpose = _FLIP_TO_PIL_TRANSPOSE.get(flip)
    return image.transpose(transpose) if transpose is not None else image
