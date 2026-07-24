"""Shared JPEG writing utilities and ICC profiles.

Shared by `imaging.master` (master JPEG) and `imaging.print_engine`
(positive JPEG) to avoid any divergence between the two writers.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

_RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"


@lru_cache(maxsize=2)
def icc_bytes(colorspace: str) -> bytes:
    """ICC profile to embed: Gray Gamma 2.2 or sRGB IEC61966-2.1."""
    name = "gray_gamma_2_2.icc" if colorspace == "gray" else "srgb.icc"
    return (_RESOURCES_DIR / name).read_bytes()


def resize_long_edge(image: Image.Image, long_edge_px: int) -> Image.Image:
    """Resizes to `long_edge_px` on the long edge (0 = full size)."""
    if long_edge_px <= 0:
        return image
    width, height = image.size
    long_edge = max(width, height)
    if long_edge == long_edge_px:
        return image
    scale = long_edge_px / long_edge
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def write_jpeg_atomic(image: Image.Image, path: Path, *, quality: int, icc_profile: bytes) -> None:
    """Writes a JPEG via a temp file + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    image.save(tmp_path, format="JPEG", quality=quality, icc_profile=icc_profile)
    tmp_path.replace(path)


def write_jpeg_positive(
    positive16: np.ndarray, path: Path, *, quality: int, long_edge_px: int
) -> None:
    """Writes `JPEG_POSITIVE/<NAME><suffix>.jpg` from a 16-bit mono array."""
    pixels8 = (positive16 // 257).astype(np.uint8)
    image = Image.fromarray(pixels8, mode="L")
    image = resize_long_edge(image, long_edge_px)
    write_jpeg_atomic(image, path, quality=quality, icc_profile=icc_bytes("gray"))
