"""Production `ExportRunner`.

Assembles `imaging.master`, `imaging.print_engine`, and `metadata.writer`
behind the `core.queue.ExportRunner` interface, so `core.queue`/
`core.session` never need to know about these modules. An expensive RAW
development + geometry pass (~1-2 s) is cached across the three tasks
(`tiff`/`jpeg_master`/`jpeg_positive`) of the same image — the cache key
includes everything that affects the shared array, so it self-invalidates
as soon as a parameter changes.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from scanassistant.core.queue import (
    ContentFrameOutcome,
    ExportContext,
    ExportFailure,
    ExportResult,
    ExportTask,
)
from scanassistant.imaging import master as master_pipeline
from scanassistant.imaging import print_engine
from scanassistant.imaging.geometry import FrameGeometry, apply_geometry
from scanassistant.imaging.jpeg_io import write_jpeg_positive
from scanassistant.imaging.raw import RawDecoder
from scanassistant.journal.journal import Journal
from scanassistant.metadata.writer import MetadataWriter, ProductionInfo
from scanassistant.project import positive_linear_cache
from scanassistant.project.campaign import Campaign
from scanassistant.project.layout import CampaignPaths

_CacheKey = tuple[
    str, str, tuple[int, int, int, int, float], str, str, tuple[int, int], tuple[float, ...] | None
]
_WRITE_ATTEMPTS = 2  # E-06: two attempts, then the image is flagged ERROR


def _final_dimensions(values: list[int]) -> tuple[int, int]:
    width, height = values
    return (width, height)


class MasterExportRunner:
    """Production implementation of `ExportRunner`."""

    def __init__(
        self,
        *,
        decoder: RawDecoder,
        campaign: Campaign,
        paths: CampaignPaths,
        metadata_writer: MetadataWriter,
        journal: Journal,
    ) -> None:
        self._decoder = decoder
        self._campaign = campaign
        self._paths = paths
        self._metadata_writer = metadata_writer
        self._journal = journal
        self._cache_key: _CacheKey | None = None
        self._cached_master: master_pipeline.DevelopedMaster | None = None

    def run(self, task: ExportTask) -> ExportResult | ExportFailure | None:
        context = task.context
        if context is None:
            # `CaptureSession` rebuilds every queued task's context from the
            # journal before it ever reaches a worker (`ExportContext` is
            # never persisted to `state.json` as-is) — a task still missing
            # one here means that reconstruction genuinely couldn't find
            # anything to rebuild from (RAW gone, or no frame ever
            # journaled for this name). An explicit failure, not a silent
            # no-op: the image was previously being treated as if this
            # export had succeeded, with nothing on disk to show for it and
            # no way for the operator to notice.
            message = "no regenerable frame for this export (RAW missing or never framed)"
            self._journal.log(
                "METADATA",
                "missing",
                image=task.name,
                level="warn",
                details={"reason": "no_export_context", "kind": task.kind},
                result="error",
            )
            return ExportFailure(code="E-06", message=message)

        try:
            master = self._developed_master(task.name, context)
        except Exception as exc:  # unreadable/corrupt RAW (E-05): image is
            # skipped, the rest of the queue keeps running rather than crash.
            message = str(exc)
            self._journal.log(
                "SYSTEM",
                "error",
                image=task.name,
                level="error",
                details={"code": "E-05", "message": message},
                result="error",
            )
            return ExportFailure(code="E-05", message=message)

        try:
            path, content_frame = self._write_kind_with_retry(task.name, task.kind, master, context)
        except Exception as exc:  # export write failure (E-06): same
            # handling as E-05, non-blocking for other tasks.
            message = str(exc)
            self._journal.log(
                "SYSTEM",
                "error",
                image=task.name,
                level="error",
                details={"code": "E-06", "message": message, "kind": task.kind},
                result="error",
            )
            return ExportFailure(code="E-06", message=message)

        self._write_metadata(task.name, context, path)
        return ExportResult(
            scale_factor=master.scale_factor,
            bounds_adjusted=master.bounds_adjusted,
            content_frame=content_frame,
        )

    def _write_kind_with_retry(
        self, name: str, kind: str, master: master_pipeline.DevelopedMaster, context: ExportContext
    ) -> tuple[Path, ContentFrameOutcome | None]:
        """E-06: two attempts before treating the write as failed."""
        last_exc: Exception | None = None
        for _attempt in range(_WRITE_ATTEMPTS):
            try:
                return self._write_kind(name, kind, master, context)
            except Exception as exc:  # noqa: BLE001 — retried, then re-raised as-is
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def _write_kind(
        self, name: str, kind: str, master: master_pipeline.DevelopedMaster, context: ExportContext
    ) -> tuple[Path, ContentFrameOutcome | None]:
        if kind == "tiff":
            path = self._paths.tiff_dir / f"{name}.tif"
            master_pipeline.write_tiff(
                master,
                path,
                bits=self._campaign.exports.tiff.bits,
                compression=self._campaign.exports.tiff.compression,
            )
            return path, None

        if kind == "jpeg_master":
            path = self._paths.jpeg_master_dir / f"{name}.jpg"
            master_pipeline.write_jpeg_master(
                master,
                path,
                quality=self._campaign.exports.jpeg_master.quality,
                long_edge_px=self._campaign.exports.jpeg_master.long_edge_px,
            )
            return path, None

        if kind == "jpeg_positive":
            positive_cfg = self._campaign.exports.jpeg_positive
            path = self._paths.jpeg_positive_dir / f"{name}{positive_cfg.suffix}.jpg"
            outcome = self._write_jpeg_positive_print_engine(name, path, master, context)
            return path, outcome

        raise ValueError(f"unknown export kind: {kind!r}")

    def _write_jpeg_positive_print_engine(
        self, name: str, path: Path, master: master_pipeline.DevelopedMaster, context: ExportContext
    ) -> ContentFrameOutcome:
        """Density-domain render, own linear RAW development — never derived
        from `master.pixels` (already gamma-encoded/sRGB, geometry only).
        `master` is used solely for its already-computed output dimensions,
        to express the content crop as an x/y/width/height/fraction shape —
        the geometry step (crop/rotate/scale) depends only on `frame`/
        `rotation_deg`/`size_mode`/`final_dimensions_px`, so
        `master.pixels.shape` is a valid stand-in without a second geometry
        pass.

        Decoded here (not via `print_engine.render_print`, which would
        hide the intermediate linear array) so this — the default
        single-worker export path every capture goes through, not just a
        campaign with a positive-finalize pool configured — also seeds
        `positive_linear_cache` for the calibration screen, the same as
        `core.positive_finalize_runner` already does."""
        positive_cfg = self._campaign.exports.jpeg_positive
        framing = self._campaign.framing
        frame = FrameGeometry(
            x=context.x,
            y=context.y,
            width=context.width,
            height=context.height,
            angle_deg=context.angle_deg,
        )
        overrides = print_engine.ManualPrintOverrides(
            dmin=context.manual_print_dmin,
            exposure_shift=context.manual_print_exposure_shift,
            contrast=context.manual_print_contrast,
            paper_black=context.manual_print_paper_black,
            paper_soft_clip=context.manual_print_paper_soft_clip,
            content_frame=context.manual_print_content_frame,
            content_frame_angle_deg=context.manual_print_content_frame_angle_deg,
        )
        final_dimensions_px = _final_dimensions(framing.final_dimensions_px)
        user_wb = self._campaign.imaging.white_balance
        development = self._decoder.develop(context.raw_path, user_wb=user_wb, linear=True)
        geometry = apply_geometry(
            development.pixels,
            frame,
            rotation_deg=context.rotation_deg,
            size_mode=framing.size_mode,
            final_dimensions_px=final_dimensions_px,
        )
        linear = geometry.pixels.astype(np.float64) / 65535.0
        result = print_engine.render_print_from_linear(
            linear,
            geometry.frame_in_output,
            overrides=overrides,
            horizontal_flip=positive_cfg.horizontal_flip,
        )
        support_height, support_width = result.support_shape
        cx, cy, cw, ch = result.content_frame
        # Only an *automatic* detection belongs in this cache (see
        # `positive_linear_cache`'s own contract) — when `overrides.
        # content_frame` was set, `result.content_mask_source` is
        # `"manual"`, and caching it would let a later cache hit report an
        # operator-confirmed crop as if it were still automatic.
        cached_content_frame = (
            (cx / support_width, cy / support_height, cw / support_width, ch / support_height)
            if overrides.content_frame is None and support_width > 0 and support_height > 0
            else None
        )
        cached_content_mask_source = (
            result.content_mask_source if overrides.content_frame is None else None
        )
        positive_linear_cache.save(
            self._paths,
            name,
            linear,
            geometry.frame_in_output,
            positive_linear_cache.DecodeFingerprint.for_decode(
                raw_path=context.raw_path,
                frame=frame,
                rotation_deg=context.rotation_deg,
                size_mode=framing.size_mode,
                final_dimensions_px=final_dimensions_px,
                white_balance=user_wb,
            ),
            content_frame=cached_content_frame,
            content_mask_source=cached_content_mask_source,
        )
        write_jpeg_positive(
            result.pixels,
            path,
            quality=positive_cfg.quality,
            long_edge_px=positive_cfg.long_edge_px,
        )
        x, y, w, h = cx, cy, cw, ch
        master_height, master_width = master.pixels.shape[:2]
        support_area = master_width * master_height
        return ContentFrameOutcome(
            x=x,
            y=y,
            width=w,
            height=h,
            fill=1.0,
            area_ratio=(w * h) / support_area if support_area > 0 else 0.0,
            source="manual" if result.content_mask_source == "manual" else "auto",
            tonal_flagged=result.flagged,
            fraction=(
                (x / master_width, y / master_height, w / master_width, h / master_height)
                if master_width > 0 and master_height > 0
                else None
            ),
            angle_deg=result.content_frame_angle_deg,
        )

    def _developed_master(
        self, name: str, context: ExportContext
    ) -> master_pipeline.DevelopedMaster:
        framing = self._campaign.framing
        exports = self._campaign.exports
        white_balance = self._campaign.imaging.white_balance
        key: _CacheKey = (
            name,
            context.rotation_deg,
            (context.x, context.y, context.width, context.height, context.angle_deg),
            exports.tiff.colorspace,
            framing.size_mode,
            _final_dimensions(framing.final_dimensions_px),
            tuple(white_balance) if white_balance is not None else None,
        )
        if self._cache_key == key and self._cached_master is not None:
            return self._cached_master

        frame = FrameGeometry(
            x=context.x,
            y=context.y,
            width=context.width,
            height=context.height,
            angle_deg=context.angle_deg,
        )
        master = master_pipeline.develop_master(
            self._decoder,
            context.raw_path,
            frame,
            rotation_deg=context.rotation_deg,
            size_mode=framing.size_mode,
            final_dimensions_px=_final_dimensions(framing.final_dimensions_px),
            colorspace=exports.tiff.colorspace,
            user_wb=white_balance,
        )
        self._cache_key = key
        self._cached_master = master
        return master

    def _write_metadata(self, name: str, context: ExportContext, derivative_path: Path) -> None:
        production = ProductionInfo(name=name, source_file=context.source_file)
        try:
            self._metadata_writer.write(
                context.raw_path, derivative_path, iptc=self._campaign.iptc, production=production
            )
        except Exception as exc:  # non-blocking by design: cause is
            # unpredictable (missing binary, subprocess failure, rejected tag...).
            self._journal.log(
                "METADATA",
                "missing",
                image=name,
                level="warn",
                details={"reason": str(exc), "file": str(derivative_path)},
                result="error",
            )
            return
        self._journal.log("METADATA", "written", image=name, details={"file": str(derivative_path)})
