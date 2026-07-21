"""Session-wide white balance: `rawpy` `user_wb` multipliers derived from a
single neutral point picked by the operator, instead of the camera's own
per-shot white balance.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from scanassistant.imaging.raw import RawDecoder

_PATCH_RADIUS_PX = 20
_MIN_CHANNEL_VALUE = 1.0  # guards against a division by (near) zero on a picked black point


def sample_white_balance(decoder: RawDecoder, raw_path: Path, x: int, y: int) -> list[float]:
    """Develops `raw_path` with no white balance applied (`user_wb=[1,1,1,1]`),
    takes the per-channel median of a patch around `(x, y)` (reference space,
    same as `FrameResult`), and returns `[R, G1, B, G2]` multipliers —
    normalized to G=1 — that make that patch neutral. Meant to be sampled
    once, from a neutral point (the light table background) in the session's
    first captured image, and reused as-is for the rest of the session: the
    illuminant doesn't change, only what's photographed against it.

    The patch is deliberately wide at full sensor resolution: the operator
    aims on a preview that is itself downscaled (and, depending on which
    pick this is, downscaled by very different amounts — an embedded
    thumbnail for the first pick of the session, a half-size development for
    every later one), so a couple of screen pixels of aim can land tens of
    reference pixels away from one pick to the next. The median (not a mean)
    keeps that wide patch from being thrown off by a stray dust speck, film
    grain, or a hot pixel caught inside it.
    """
    development = decoder.develop(raw_path, user_wb=[1.0, 1.0, 1.0, 1.0])
    pixels = development.pixels.astype(np.float64)
    height, width = pixels.shape[:2]
    # Clamped, not just clipped: a point outside the image entirely (preview
    # scale-factor rounding, or a stale scale factor from a differently-sized
    # development) must still land on a real, non-empty patch at the edge.
    x = min(max(x, 0), width - 1)
    y = min(max(y, 0), height - 1)
    x0, x1 = max(0, x - _PATCH_RADIUS_PX), min(width, x + _PATCH_RADIUS_PX + 1)
    y0, y1 = max(0, y - _PATCH_RADIUS_PX), min(height, y + _PATCH_RADIUS_PX + 1)
    patch = pixels[y0:y1, x0:x1]
    r, g, b = (
        max(float(np.median(patch[:, :, channel])), _MIN_CHANNEL_VALUE) for channel in range(3)
    )
    return [g / r, 1.0, g / b, 1.0]
