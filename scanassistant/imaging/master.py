"""Master pipeline: development, geometry, TIFF and master JPEG.

Produces the shared 16-bit array (`DevelopedMaster.pixels`) that feeds the
TIFF, the master JPEG, and the reading positive (`imaging.positive`) —
same array in memory, no possible geometry divergence. No dependency on
PySide6.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

from scanassistant.imaging.geometry import FrameGeometry, apply_geometry
from scanassistant.imaging.jpeg_io import icc_bytes, resize_long_edge, write_jpeg_atomic
from scanassistant.imaging.raw import RawDecoder

_REC709 = np.array([0.2126, 0.7152, 0.0722])


@dataclass(frozen=True)
class DevelopedMaster:
    """Shared 16-bit array, post-geometry (and post-colorspace conversion if any)."""

    pixels: np.ndarray  # (H, W, 3) uint16 if colorspace=srgb, (H, W) uint16 if gray
    scale_factor: float
    bounds_adjusted: bool  # fixed mode only


def develop_master(
    decoder: RawDecoder,
    raw_path: Path,
    frame: FrameGeometry,
    *,
    orientation: str,
    size_mode: str,
    final_dimensions_px: tuple[int, int],
    colorspace: str,
) -> DevelopedMaster:
    """RAW 16-bit development + geometry."""
    development = decoder.develop(raw_path)
    geometry = apply_geometry(
        development.pixels,
        frame,
        orientation=orientation,
        size_mode=size_mode,
        final_dimensions_px=final_dimensions_px,
    )
    pixels = geometry.pixels
    if colorspace == "gray":
        pixels = _to_luminance16(pixels)
    return DevelopedMaster(
        pixels=pixels,
        scale_factor=geometry.scale_factor,
        bounds_adjusted=geometry.bounds_adjusted,
    )


def write_tiff(master: DevelopedMaster, path: Path, *, bits: int, compression: str) -> None:
    """Writes `TIFF/<NAME>.tif`."""
    colorspace = "gray" if master.pixels.ndim == 2 else "srgb"
    pixels = _reduce_bits(master.pixels, bits)
    photometric = "minisblack" if pixels.ndim == 2 else "rgb"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tifffile.imwrite(
        tmp_path,
        pixels,
        photometric=photometric,
        compression=None if compression == "none" else compression,
        iccprofile=icc_bytes(colorspace),
    )
    tmp_path.replace(path)


def write_jpeg_master(
    master: DevelopedMaster, path: Path, *, quality: int, long_edge_px: int
) -> None:
    """Writes `JPEG_MASTER/<NAME>.jpg`."""
    colorspace = "gray" if master.pixels.ndim == 2 else "srgb"
    pixels8 = _reduce_bits(master.pixels, 8)
    image = Image.fromarray(pixels8, mode="L" if pixels8.ndim == 2 else "RGB")
    image = resize_long_edge(image, long_edge_px)
    write_jpeg_atomic(image, path, quality=quality, icc_profile=icc_bytes(colorspace))


def _to_luminance16(pixels: np.ndarray) -> np.ndarray:
    """Rec.709 luminance, 16-bit."""
    if pixels.ndim == 2:
        return pixels
    luminance = pixels.astype(np.float64) @ _REC709
    return np.clip(np.round(luminance), 0, 65535).astype(np.uint16)


def _reduce_bits(pixels: np.ndarray, bits: int) -> np.ndarray:
    if bits == 16:
        return pixels
    return (pixels // 257).astype(np.uint8)
