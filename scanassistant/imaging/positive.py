"""Reading-positive pipeline.

Starts from the same 16-bit array as the masters, after geometry
(`imaging.master.DevelopedMaster.pixels`). `auto` mode isolates its
optimization step behind `_auto` to allow a future alternative backend
without touching the rest of the pipeline. No dependency on PySide6.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from scanassistant.imaging.jpeg_io import icc_bytes, resize_long_edge, write_jpeg_atomic

_REC709 = np.array([0.2126, 0.7152, 0.0722])

MODE_SIMPLE = "simple"
MODE_AUTO = "auto"
MODE_MANUAL = "manual"


@dataclass(frozen=True)
class ManualSettings:
    exposure_ev: float = 0.0
    contrast: int = 0
    shadows: int = 0
    highlights: int = 0


def render_positive(
    pixels: np.ndarray,
    *,
    horizontal_flip: bool = True,
    mode: str = MODE_AUTO,
    manual: ManualSettings | None = None,
) -> np.ndarray:
    """16-bit monochrome positive array, before 8-bit conversion."""
    array = np.fliplr(pixels) if horizontal_flip else pixels
    inverted = (65535 - array.astype(np.int32)).astype(np.uint16)
    luminance = _to_luminance(inverted)

    v = luminance.astype(np.float64) / 65535.0
    if mode == MODE_SIMPLE:
        v = _simple(v)
    elif mode == MODE_MANUAL:
        v = _manual(v, manual or ManualSettings())
    else:
        v = _auto(v)
    return np.clip(np.round(v * 65535.0), 0, 65535).astype(np.uint16)


def write_jpeg_positive(
    positive16: np.ndarray, path: Path, *, quality: int, long_edge_px: int
) -> None:
    """Writes `JPEG_POSITIVE/<NAME>-POS.jpg`."""
    pixels8 = (positive16 // 257).astype(np.uint8)
    image = Image.fromarray(pixels8, mode="L")
    image = resize_long_edge(image, long_edge_px)
    write_jpeg_atomic(image, path, quality=quality, icc_profile=icc_bytes("gray"))


def _to_luminance(array: np.ndarray) -> np.ndarray:
    if array.ndim == 2:
        return array
    luminance = array.astype(np.float64) @ _REC709
    return np.clip(np.round(luminance), 0, 65535).astype(np.uint16)


def _simple(v: np.ndarray) -> np.ndarray:
    """Linear min-max normalization, nothing else."""
    lo, hi = float(v.min()), float(v.max())
    if hi <= lo:
        return np.zeros_like(v)
    return (v - lo) / (hi - lo)


def _auto(v: np.ndarray) -> np.ndarray:
    """Percentile stretch + CLAHE + adaptive gamma."""
    p_low, p_high = np.percentile(v, [0.5, 99.5])
    stretched = np.clip((v - p_low) / (p_high - p_low), 0.0, 1.0) if p_high > p_low else v

    stretched16 = np.clip(stretched * 65535.0, 0, 65535).astype(np.uint16)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    equalized = clahe.apply(stretched16).astype(np.float64) / 65535.0

    mean = float(equalized.mean())
    if mean <= 0:
        return equalized
    gamma = float(np.clip(np.log(0.45) / np.log(mean), 0.5, 2.2))
    return np.power(equalized, gamma)


def _manual(v: np.ndarray, settings: ManualSettings) -> np.ndarray:
    """Campaign settings, fixed formulas."""
    v = np.clip(v * (2.0**settings.exposure_ev), 0.0, 1.0)

    if settings.shadows:
        s = settings.shadows
        v = np.clip(v + (np.power(v, 1 / (1 + s / 100)) - v) * (1 - v) ** 2, 0.0, 1.0)

    if settings.highlights:
        h = settings.highlights
        v = np.clip(v + (np.power(v, 1 + h / 100) - v) * v**2, 0.0, 1.0)

    if settings.contrast:
        c = settings.contrast
        v = np.clip(0.5 + (v - 0.5) * (1 + c / 100), 0.0, 1.0)

    return v
