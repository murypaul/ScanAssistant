"""Reading-positive pipeline: reconstructs the darkroom print process
(density relative to the film's own base, a film response curve, then a
paper response) instead of stretching an already inverted, gamma-encoded
array.

Starts from a *linear* RAW development (`imaging.raw.RawDecoder.develop(...,
linear=True)`), then the same geometry crop the master pipeline uses
(`imaging.geometry.apply_geometry`) — this module never sees the unexposed
light table outside the support frame, only what geometry already cropped
to. Reuses the existing content-frame detector (`imaging.content_framing`,
GrabCut) for the region density statistics are drawn from, rather than a
new detector. No dependency on PySide6.

`render_print_from_linear` is the core, directly testable on synthetic
arrays; `render_print` is the thin RAW/geometry orchestration around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from scanassistant.imaging.content_framing import detect_content_frame
from scanassistant.imaging.geometry import FrameGeometry, apply_geometry
from scanassistant.imaging.raw import RawDecoder

_REC709 = np.array([0.2126, 0.7152, 0.0722])
_THRESHOLD = 1.0 / 65535.0  # guards log(0) on a pixel at or above Dmin

# Film response (fixed — a property of the film stock, not recomputed per
# image): toe/shoulder softplus compression on normalized density.
_FILM_TOE_STRENGTH = 6.0
_FILM_SHOULDER_STRENGTH = 6.0

# Paper response: contrast and exposure are computed separately (bundling
# them into one pivot/stretch was found, empirically, to misdiagnose which
# correction a given negative actually needs).
_CONTRAST_TARGET_SPREAD = 0.75
_CONTRAST_CAP_LOW = 0.6
_CONTRAST_CAP_HIGH = 2.2
_TARGET_MEAN = 0.45  # empirically a well-exposed reading positive's own mean
_EXPOSURE_SHIFT_FLAG = 0.20
DEFAULT_PAPER_BLACK = 0.02
DEFAULT_PAPER_SOFT_CLIP = 0.85

# Dmin ring / content-mask insets, fractions of the support frame's own
# width/height.
_BORDER_INSET_FRACTION = 0.015
_BORDER_RING_WIDTH_FRACTION = 0.07
_CONTENT_INSET_FRACTION = 0.15
_DENSITY_PERCENTILE_HIGH = 99.5

# Local contrast (bilateral filter — not CLAHE, which was found to force
# contrast into flat/uniform regions instead of leaving them alone): only
# ever applied to images the confidence signal already flagged, never as a
# default step.
_BILATERAL_SIGMA_COLOR = 0.12
_BILATERAL_SIGMA_SPACE = 15.0
_BILATERAL_DETAIL_BOOST = 2.2


@dataclass(frozen=True)
class ManualPrintOverrides:
    """Explicit per-group manual overrides: each field maps 1:1 to one of
    the calibration screen's groups — film base (Dmin), scan exposure,
    paper model (contrast/black/soft-clip) — `None` means that group stays
    automatic. No field for the film model group (toe/shoulder): fixed as
    a property of the film stock, not recomputed per image — the screen
    surfaces it read-only, nothing to override here."""

    dmin: tuple[float, float, float] | None = None
    exposure_shift: float | None = None
    contrast: float | None = None
    paper_black: float | None = None
    paper_soft_clip: float | None = None
    # x, y, w, h *fractions* of `linear`'s own width/height — the support
    # frame, pre-flip (same convention `PositiveOverride.print_content_
    # frame` persists) — fractions, not absolute pixels, since the caller
    # (a persisted override) is bound to a resolution-independent value,
    # not this particular decode's own pixel dimensions. `None` keeps the
    # automatic GrabCut/inset detection (`_content_mask`). Unlike the other
    # fields, this one has no corresponding calibration-screen "group":
    # it's set by dragging the crop overlay, not a slider.
    content_frame: tuple[float, float, float, float] | None = None
    # Deskew, degrees, same convention as `imaging.geometry.FrameGeometry.
    # angle_deg` (Capture's own crop rotation, Ctrl+Left/Right there) —
    # only meaningful alongside a set `content_frame` (an operator's own
    # crop): the automatic GrabCut/inset detection never produces one.
    # Applied only at the final crop (a real `warpAffine`, like `imaging.
    # geometry._deskew_and_crop`), never to the density/GrabCut statistics
    # mask, which stays the crop's plain axis-aligned bounding box — a
    # rotated content region's stats mask including a few extra corner
    # pixels near the border is already filtered out by `_content_mask`'s
    # own density-based border refinement, the same tolerance an
    # unrotated crop already relies on.
    content_frame_angle_deg: float = 0.0


_AUTO = ManualPrintOverrides()


@dataclass(frozen=True)
class PrintResult:
    """16-bit monochrome positive, plus the diagnostics the calibration
    screen and the journal need — never silently dropped."""

    pixels: np.ndarray  # (H, W) uint16, monochrome, already cropped to `content_frame`
    dmin: tuple[float, float, float]
    dmax: float
    contrast: float
    raw_contrast: float
    exposure_shift: float
    flagged: bool
    content_mask_source: str  # "grabcut" | "inset_fallback" | "manual"
    local_contrast_applied: bool
    content_frame: tuple[int, int, int, int]  # x, y, w, h in the geometry
    # (post support-frame-crop) coordinate space `render_print_from_linear`
    # received — same space `imaging.content_framing.ContentFrameResult`
    # already uses elsewhere, so callers can build a `ContentFrameOutcome`
    # from it directly.
    support_shape: tuple[int, int]  # (height, width) of that same coordinate
    # space — callers building a `ContentFrameOutcome.fraction` need this as
    # the denominator; `imaging.master.DevelopedMaster.pixels.shape` is a
    # valid stand-in when one is already available (same geometry params),
    # but a caller with no master at all (`core.positive_finalize_runner`)
    # needs it from here instead of a redundant second geometry pass.
    content_frame_angle_deg: float = 0.0  # echoes `ManualPrintOverrides.
    # content_frame_angle_deg` (0.0 for an automatic detection — GrabCut/
    # inset never rotate) — same mirrored-on-flip convention as
    # `content_frame` itself in the `crop_to_content=False` preview space.


def render_print(
    decoder: RawDecoder,
    raw_path: Path,
    frame: FrameGeometry,
    *,
    rotation_deg: int = 0,
    size_mode: str = "native",
    final_dimensions_px: tuple[int, int] = (6016, 4016),
    user_wb: list[float] | None = None,
    overrides: ManualPrintOverrides = _AUTO,
    horizontal_flip: bool = True,
    crop_to_content: bool = True,
) -> PrintResult:
    """RAW linear development + geometry, then `render_print_from_linear`."""
    development = decoder.develop(raw_path, user_wb=user_wb, linear=True)
    geometry = apply_geometry(
        development.pixels,
        frame,
        rotation_deg=rotation_deg,
        size_mode=size_mode,
        final_dimensions_px=final_dimensions_px,
    )
    linear = geometry.pixels.astype(np.float64) / 65535.0
    return render_print_from_linear(
        linear,
        geometry.frame_in_output,
        overrides=overrides,
        horizontal_flip=horizontal_flip,
        crop_to_content=crop_to_content,
    )


def render_print_from_linear(
    linear: np.ndarray,
    frame_in_output: FrameGeometry,
    *,
    overrides: ManualPrintOverrides = _AUTO,
    horizontal_flip: bool = True,
    crop_to_content: bool = True,
    cached_content_frame: tuple[float, float, float, float] | None = None,
    cached_content_mask_source: str | None = None,
) -> PrintResult:
    """Core algorithm on an already linear, already geometry-cropped array
    — `linear` is RGB, float64, [0, 1].

    Any `overrides` field left `None` computes exactly as before (fully
    automatic); a set field short-circuits that group's own computation.
    `flagged` (the tonal-confidence signal) only ever reflects
    groups still on auto — once an operator has set a value by hand, its
    own automatic estimate being out of bounds is no longer a reason to
    ask them to look at it again.

    `cached_content_frame`/`cached_content_mask_source` (x/y/w/h fractions
    of `linear`'s own width/height, plus the detector name that produced
    them — same convention as `overrides.content_frame`, but from a prior
    *automatic* detection, e.g. `project.positive_linear_cache`, not an
    operator confirmation): skips GrabCut/inset detection and reuses that
    rectangle verbatim, still reporting the real detector as the source —
    never `"manual"` — so review/completeness signals stay accurate. Only
    consulted when `overrides.content_frame is None`; an operator override
    always wins.

    `crop_to_content=False` (the calibration screen's crop-editing preview
    only — every export path keeps the default) skips the final crop: the
    full support-frame positive is returned instead, and `content_frame`
    is expressed in *that same, already-flipped* array's own coordinate
    space (mirrored across the frame's width when `horizontal_flip`, unlike
    the default crop-then-return contract's pre-flip convention) — so a
    caller can draw/drag it directly over `pixels` without its own
    transform. Never mixed with the default: two different coordinate
    conventions for the same field would be a much worse trap than the
    extra parameter."""
    dmin = (
        np.array(overrides.dmin, dtype=np.float64)
        if overrides.dmin is not None
        else _sample_dmin(linear, frame_in_output)
    )
    manual_rect: tuple[int, int, int, int] | None = None
    if overrides.content_frame is not None:
        full_h, full_w = linear.shape[:2]
        xf, yf, wf, hf = overrides.content_frame
        manual_rect = (
            round(xf * full_w),
            round(yf * full_h),
            max(1, round(wf * full_w)),
            max(1, round(hf * full_h)),
        )
    cached_rect: tuple[int, int, int, int] | None = None
    if manual_rect is None and cached_content_frame is not None:
        full_h, full_w = linear.shape[:2]
        xf, yf, wf, hf = cached_content_frame
        cached_rect = (
            round(xf * full_w),
            round(yf * full_h),
            max(1, round(wf * full_w)),
            max(1, round(hf * full_h)),
        )
    mask, mask_source, content_frame = _content_mask(
        linear, frame_in_output, dmin, manual_rect, cached_rect, cached_content_mask_source
    )
    # Only an operator's own crop can carry a deskew — the automatic
    # detection (`_content_mask`'s GrabCut/inset fallback) never rotates.
    content_frame_angle_deg = (
        float(overrides.content_frame_angle_deg) if manual_rect is not None else 0.0
    )

    density = np.log10(dmin[None, None, :] / np.maximum(linear, _THRESHOLD))
    density = np.clip(density, 0.0, None)
    dmax = max(float(np.percentile(density[mask], _DENSITY_PERCENTILE_HIGH)), 1e-3)
    d_norm = np.clip(density / dmax, 0.0, 1.0)

    v = _film_curve(d_norm)

    v_content = v[mask]
    mean_before = float(np.mean(v_content))
    low, high = np.percentile(v_content, [5.0, 95.0])
    spread = max(high - low, 1e-3)
    raw_contrast = _CONTRAST_TARGET_SPREAD / spread
    if overrides.contrast is not None:
        contrast = float(overrides.contrast)
        flagged = False
    else:
        contrast = float(np.clip(raw_contrast, _CONTRAST_CAP_LOW, _CONTRAST_CAP_HIGH))
        flagged = not (_CONTRAST_CAP_LOW <= raw_contrast <= _CONTRAST_CAP_HIGH)
    v = np.clip(mean_before + (v - mean_before) * contrast, 0.0, 1.0)

    mean_after_contrast = float(np.mean(v[mask]))
    if overrides.exposure_shift is not None:
        exposure_shift = float(overrides.exposure_shift)
    else:
        exposure_shift = _TARGET_MEAN - mean_after_contrast
        flagged = flagged or abs(exposure_shift) > _EXPOSURE_SHIFT_FLAG
    v = np.clip(v + exposure_shift, 0.0, 1.0)

    # Paper black/soft-clip, per channel (R, G, B) — same order the darkroom
    # print process itself follows (paper responds to the exposing light
    # before any monochrome conversion exists to speak of) and what the
    # normative spec for this engine describes: the whole paper model runs
    # before the monochrome step below, not after.
    paper_black = (
        DEFAULT_PAPER_BLACK if overrides.paper_black is None else float(overrides.paper_black)
    )
    paper_soft_clip = (
        DEFAULT_PAPER_SOFT_CLIP
        if overrides.paper_soft_clip is None
        else float(overrides.paper_soft_clip)
    )
    v = np.clip(v + paper_black, 0.0, None)
    clipped = -(v - paper_soft_clip) / max(1e-6, 1.0 - paper_soft_clip)
    above = v > paper_soft_clip
    v = np.where(above, paper_soft_clip + (1 - np.exp(clipped)) * (1 - paper_soft_clip), v)
    v = np.clip(v, 0.0, 1.0)

    luminance = v @ _REC709

    local_contrast_applied = False
    if flagged:
        luminance = _local_contrast_bilateral(luminance)
        local_contrast_applied = True

    pixels16 = np.clip(np.round(luminance * 65535.0), 0, 65535).astype(np.uint16)
    full_height, full_width = pixels16.shape[:2]
    if crop_to_content:
        cx, cy, cw, ch = content_frame
        if content_frame_angle_deg != 0.0:
            # True deskew, same approach `imaging.geometry._deskew_and_crop`
            # already uses for the support frame: rotate the whole array
            # around the crop's own center so the crop becomes axis-aligned
            # in the rotated result, then take the same (cx, cy, cw, ch)
            # slice — center-preserving, so it's still exactly this rect.
            center = (cx + cw / 2, cy + ch / 2)
            matrix = cv2.getRotationMatrix2D(center, content_frame_angle_deg, 1.0)
            pixels16 = cv2.warpAffine(
                pixels16,
                matrix,
                (full_width, full_height),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
        pixels16 = pixels16[cy : cy + ch, cx : cx + cw]
        if horizontal_flip:
            # Negatives are captured emulsion-side up (more detail) —
            # geometrically mirrored left-right versus the actual scene.
            # Done last, on the already-cropped output, so `content_frame`
            # stays in the un-mirrored support-frame coordinate space other
            # callers expect.
            pixels16 = np.fliplr(pixels16)
    else:
        if horizontal_flip:
            pixels16 = np.fliplr(pixels16)
            cx, cy, cw, ch = content_frame
            content_frame = (full_width - cx - cw, cy, cw, ch)
            # A horizontal mirror reverses the sense of rotation too — same
            # correction `content_frame`'s own x just went through.
            content_frame_angle_deg = -content_frame_angle_deg

    return PrintResult(
        pixels=pixels16,
        dmin=(float(dmin[0]), float(dmin[1]), float(dmin[2])),
        dmax=dmax,
        contrast=contrast,
        raw_contrast=float(raw_contrast),
        exposure_shift=float(exposure_shift),
        flagged=flagged,
        content_mask_source=mask_source,
        local_contrast_applied=local_contrast_applied,
        content_frame=content_frame,
        support_shape=(linear.shape[0], linear.shape[1]),
        content_frame_angle_deg=content_frame_angle_deg,
    )


def _softplus(x: np.ndarray, k: float) -> np.ndarray:
    return np.log1p(np.exp(np.clip(k * x, -50, 50))) / k


def _film_curve(d_norm: np.ndarray) -> np.ndarray:
    """Toe/shoulder compression on normalized density [0, 1] — a fixed
    shape, not recomputed per image: a property of the film stock."""
    toe = _softplus(d_norm, _FILM_TOE_STRENGTH) / _softplus(1.0, _FILM_TOE_STRENGTH)
    shoulder = 1.0 - _softplus(1.0 - toe, _FILM_SHOULDER_STRENGTH) / _softplus(
        1.0, _FILM_SHOULDER_STRENGTH
    )
    return np.clip(shoulder, 0.0, 1.0)


def _rect_mask(shape: tuple[int, int], x: int, y: int, w: int, h: int) -> np.ndarray:
    height, width = shape
    mask = np.zeros((height, width), dtype=bool)
    mask[max(0, y) : min(height, y + h), max(0, x) : min(width, x + w)] = True
    return mask


def _ring_mask(shape: tuple[int, int], x: int, y: int, w: int, h: int) -> np.ndarray:
    inset_x, inset_y = round(w * _BORDER_INSET_FRACTION), round(h * _BORDER_INSET_FRACTION)
    ring_x, ring_y = round(w * _BORDER_RING_WIDTH_FRACTION), round(h * _BORDER_RING_WIDTH_FRACTION)
    outer = _rect_mask(shape, x + inset_x, y + inset_y, w - 2 * inset_x, h - 2 * inset_y)
    inner = _rect_mask(
        shape,
        x + inset_x + ring_x,
        y + inset_y + ring_y,
        w - 2 * (inset_x + ring_x),
        h - 2 * (inset_y + ring_y),
    )
    return outer & ~inner


def _sample_dmin(linear: np.ndarray, frame: FrameGeometry) -> np.ndarray:
    """Robust (90th percentile, not mean/max) per-channel sample of the
    unexposed border just inside the support frame — a dust speck or
    scratch in the ring shouldn't pull the whole estimate off."""
    x, y, w, h = round(frame.x), round(frame.y), round(frame.width), round(frame.height)
    mask = _ring_mask(linear.shape[:2], x, y, w, h)
    if mask.sum() < 100:
        mask = _rect_mask(linear.shape[:2], x, y, w, h)
    return np.percentile(linear[mask], 90, axis=0)


_DMIN_PICK_RADIUS_PX = 8


def sample_dmin_at_point(linear: np.ndarray, x: int, y: int) -> tuple[float, float, float]:
    """Per-channel film-base color at an operator-picked point (the
    calibration screen's "pick from image" tool) — `x`/`y` in `linear`'s own
    coordinate space (the caller mirrors `x` first if the picked point came
    from a horizontally-flipped preview). Median over a small local window,
    not the single clicked pixel, for the same reason `_sample_dmin`'s own
    border ring uses a percentile rather than a single sample: one dust
    speck/grain under the cursor shouldn't set the whole estimate."""
    height, width = linear.shape[:2]
    y0, y1 = max(0, y - _DMIN_PICK_RADIUS_PX), min(height, y + _DMIN_PICK_RADIUS_PX + 1)
    x0, x1 = max(0, x - _DMIN_PICK_RADIUS_PX), min(width, x + _DMIN_PICK_RADIUS_PX + 1)
    patch = linear[y0:y1, x0:x1].reshape(-1, linear.shape[-1])
    r, g, b = np.median(patch, axis=0)
    return float(r), float(g), float(b)


def _content_mask(
    linear: np.ndarray,
    frame: FrameGeometry,
    dmin: np.ndarray,
    manual_rect: tuple[int, int, int, int] | None = None,
    cached_rect: tuple[int, int, int, int] | None = None,
    cached_source: str | None = None,
) -> tuple[np.ndarray, str, tuple[int, int, int, int]]:
    """Content region for the density statistics *and* the final crop.

    `manual_rect` (an operator-confirmed crop, `ManualPrintOverrides.
    content_frame`) wins outright — no GrabCut/inset fallback runs at all.
    Otherwise, `cached_rect` (a prior *automatic* detection, e.g. from
    `project.positive_linear_cache`) wins next, on the same terms, but
    reports `cached_source` instead of `"manual"` — reusing a detection is
    not an operator confirming one. Otherwise: GrabCut
    (`imaging.content_framing`, same detector already in production) when
    confident, else a rectangular inset. In every case, a second pass
    removes any pixel whose density clips to 0 on every channel from the
    *statistics* mask only (never the crop rectangle, which stays a clean
    rectangle): brighter than the measured Dmin everywhere, so by
    definition not exposed content — found on a real sample where the
    inset fallback let 30% of a support frame's own unexposed border leak
    into the "content" statistics."""
    shape = linear.shape[:2]
    if manual_rect is not None or cached_rect is not None:
        if manual_rect is not None:
            x, y, w, h = manual_rect
            rect, source = manual_rect, "manual"
        else:
            assert cached_rect is not None
            x, y, w, h = cached_rect
            rect, source = cached_rect, cached_source or "grabcut"
        mask = _rect_mask(shape, x, y, w, h)
        density = np.log10(dmin[None, None, :] / np.maximum(linear, _THRESHOLD))
        not_border = (density > 0.0).any(axis=-1)
        refined = mask & not_border
        stats_mask = refined if refined.sum() >= 1000 else mask
        return stats_mask, source, rect

    preview8 = np.clip(np.sqrt(np.clip(linear, 0, 1)) * 255, 0, 255).astype(np.uint8)
    result = detect_content_frame(preview8, frame)
    source = None
    rect = None
    if result is not None:
        candidate = _rect_mask(shape, result.x, result.y, result.width, result.height)
        if candidate.sum() >= 1000:
            mask, source = candidate, "grabcut"
            rect = (result.x, result.y, result.width, result.height)
    if source is None:
        x = round(frame.x + frame.width * _CONTENT_INSET_FRACTION)
        y = round(frame.y + frame.height * _CONTENT_INSET_FRACTION)
        w = round(frame.width * (1 - 2 * _CONTENT_INSET_FRACTION))
        h = round(frame.height * (1 - 2 * _CONTENT_INSET_FRACTION))
        mask = _rect_mask(shape, x, y, w, h)
        source = "inset_fallback"
        rect = (x, y, w, h)

    density = np.log10(dmin[None, None, :] / np.maximum(linear, _THRESHOLD))
    not_border = (density > 0.0).any(axis=-1)
    refined = mask & not_border
    # refinement too aggressive on a degenerate case: keep the coarse mask
    stats_mask = refined if refined.sum() >= 1000 else mask
    return stats_mask, source, rect


def _local_contrast_bilateral(luminance: np.ndarray) -> np.ndarray:
    """Edge-preserving local contrast boost, flagged images only — leaves
    near-uniform regions alone by construction (unlike CLAHE, which forces
    contrast into a flat tile regardless of whether there's real detail to
    recover there, measured to turn a genuinely saturated black region
    into flat mid-grey)."""
    luminance32 = luminance.astype(np.float32)
    smooth = cv2.bilateralFilter(
        luminance32, d=0, sigmaColor=_BILATERAL_SIGMA_COLOR, sigmaSpace=_BILATERAL_SIGMA_SPACE
    )
    detail = luminance32 - smooth
    boosted = smooth + detail * _BILATERAL_DETAIL_BOOST
    return np.clip(boosted, 0.0, 1.0).astype(np.float64)
