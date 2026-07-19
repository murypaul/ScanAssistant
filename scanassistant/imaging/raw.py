"""RAW decoding: interface + `rawpy` implementation.

`RawDecoder` isolates every dependency on `rawpy`/LibRaw behind a minimal
interface (thumbnail extraction, full-frame fallback) so a test double can
stand in without a real RAW file. Normalization (JPEG/bitmap decoding,
orientation, scale factor) lives in `imaging.preview`, not here: this
module only talks to LibRaw.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

ThumbFormat = Literal["jpeg", "bitmap", "none"]


@dataclass(frozen=True)
class RawDevelopment:
    """Full 16-bit development, in reference space.

    `pixels`: RGB16 array (H, W, 3), orientation already applied (default
    behavior of `rawpy.postprocess`, `user_flip=None`) — same reference
    space as `imaging.preview.Preview`.
    """

    pixels: np.ndarray


@dataclass(frozen=True)
class RawThumbnail:
    """Raw thumbnail (or full-frame fallback) as returned by the decoder.

    `data`: JPEG bytes (format == "jpeg"), or contiguous RGB8 bytes,
    width×height×3 (format == "bitmap"), empty if "none". `flip` is
    LibRaw's rotation code (`sizes.flip`: 0 = none, 3 = 180°, 5 = 90° CCW,
    6 = 90° CW) to apply to reach reference space.
    """

    format: ThumbFormat
    data: bytes = b""
    width: int = 0
    height: int = 0
    flip: int = 0
    reference_width: int = 0
    reference_height: int = 0


class RawDecoder(Protocol):
    """Test doubles can implement this instead."""

    def read_thumbnail(self, path: Path) -> RawThumbnail:
        """Embedded thumbnail (`rawpy.extract_thumb()`)."""
        ...

    def read_full_preview(self, path: Path) -> RawThumbnail:
        """Fallback: thumbnail missing or under 1024 px on the long edge."""
        ...

    def develop(self, path: Path) -> RawDevelopment:
        """Full 16-bit development (`rawpy.postprocess`)."""
        ...


def _reference_size(raw: Any) -> tuple[int, int]:
    """`raw.sizes.width/height` are pre-flip, sensor-space dimensions —
    swapped here for a 90°/270° rotation (`flip` 5 or 6) so the result
    describes the post-rotation reference space instead: the same space
    `pixels` (post `_apply_orientation`) and `RawDevelopment.pixels`
    (rawpy's own flip already applied by `postprocess`) are both in.
    """
    width, height = raw.sizes.width, raw.sizes.height
    if raw.sizes.flip in (5, 6):
        return height, width
    return width, height


class RawpyDecoder:
    """Production implementation (rawpy/LibRaw)."""

    def read_thumbnail(self, path: Path) -> RawThumbnail:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            reference_width, reference_height = _reference_size(raw)
            flip = raw.sizes.flip
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                return RawThumbnail(
                    format="none",
                    flip=flip,
                    reference_width=reference_width,
                    reference_height=reference_height,
                )

            if thumb.format is rawpy.ThumbFormat.JPEG:
                assert isinstance(thumb.data, bytes)
                return RawThumbnail(
                    format="jpeg",
                    data=thumb.data,
                    flip=flip,
                    reference_width=reference_width,
                    reference_height=reference_height,
                )

            assert isinstance(thumb.data, np.ndarray)
            height, width = thumb.data.shape[:2]
            return RawThumbnail(
                format="bitmap",
                data=thumb.data.tobytes(),
                width=width,
                height=height,
                flip=flip,
                reference_width=reference_width,
                reference_height=reference_height,
            )

    def read_full_preview(self, path: Path) -> RawThumbnail:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(
                half_size=True,
                output_bps=8,
                use_camera_wb=True,
                no_auto_bright=True,
                user_flip=0,  # rotation applied by the caller via `flip`;
                # without this, `user_flip=None` (default) would rotate the image twice.
            )
            height, width = rgb.shape[:2]
            reference_width, reference_height = _reference_size(raw)
            return RawThumbnail(
                format="bitmap",
                data=rgb.tobytes(),
                width=width,
                height=height,
                flip=raw.sizes.flip,
                reference_width=reference_width,
                reference_height=reference_height,
            )

    def develop(self, path: Path) -> RawDevelopment:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=True,
                output_bps=16,
                output_color=rawpy.ColorSpace.sRGB,
            )
            return RawDevelopment(pixels=rgb)
