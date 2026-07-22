"""Production `ExportRunner`.

Assembles `imaging.master`, `imaging.positive`, and `metadata.writer`
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
from scanassistant.imaging import positive as positive_pipeline
from scanassistant.imaging.content_framing import detect_content_frame
from scanassistant.imaging.geometry import FrameGeometry
from scanassistant.imaging.raw import RawDecoder
from scanassistant.journal.journal import Journal
from scanassistant.metadata.writer import MetadataWriter, ProductionInfo
from scanassistant.project.campaign import Campaign, JpegPositiveExportConfig
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
            # Task rebuilt cold from `state.json` without a context: not
            # regenerable here.
            self._journal.log(
                "METADATA",
                "missing",
                image=task.name,
                level="warn",
                details={"reason": "no_export_context", "kind": task.kind},
                result="error",
            )
            return None

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
            source_pixels, outcome = self._positive_source_pixels(master, context)
            mode, manual_settings = self._positive_render_settings(positive_cfg, context)
            positive16 = positive_pipeline.render_positive(
                source_pixels,
                horizontal_flip=positive_cfg.horizontal_flip,
                mode=mode,
                manual=manual_settings,
            )
            positive_pipeline.write_jpeg_positive(
                positive16,
                path,
                quality=positive_cfg.quality,
                long_edge_px=positive_cfg.long_edge_px,
            )
            return path, outcome

        raise ValueError(f"unknown export kind: {kind!r}")

    def _positive_source_pixels(
        self, master: master_pipeline.DevelopedMaster, context: ExportContext
    ) -> tuple[np.ndarray, ContentFrameOutcome | None]:
        """The array to render the positive from: an operator's manual
        content-frame choice (`context.content_frame_override`, from the
        "Recadrage des positifs" screen) always wins over automatic
        detection for this one regeneration — never recomputed against it."""
        master_height, master_width = master.pixels.shape[:2]
        if context.content_frame_override is not None:
            x_frac, y_frac, w_frac, h_frac = context.content_frame_override
            x = round(x_frac * master_width)
            y = round(y_frac * master_height)
            width = round(w_frac * master_width)
            height = round(h_frac * master_height)
            support_area = master.frame_in_output.width * master.frame_in_output.height
            outcome = ContentFrameOutcome(
                x=x,
                y=y,
                width=width,
                height=height,
                fill=1.0,
                area_ratio=(width * height) / support_area if support_area > 0 else 0.0,
                source="manual",
                fraction=(x_frac, y_frac, w_frac, h_frac),
            )
            return master.pixels[y : y + height, x : x + width], outcome

        content_frame = detect_content_frame(master.pixels, master.frame_in_output)
        if content_frame is None:
            return master.pixels, None
        outcome = ContentFrameOutcome(
            x=content_frame.x,
            y=content_frame.y,
            width=content_frame.width,
            height=content_frame.height,
            fill=content_frame.fill,
            area_ratio=content_frame.area_ratio,
            source="auto",
            fraction=(
                content_frame.x / master_width,
                content_frame.y / master_height,
                content_frame.width / master_width,
                content_frame.height / master_height,
            ),
        )
        source_pixels = master.pixels[
            content_frame.y : content_frame.y + content_frame.height,
            content_frame.x : content_frame.x + content_frame.width,
        ]
        return source_pixels, outcome

    def _positive_render_settings(
        self, positive_cfg: JpegPositiveExportConfig, context: ExportContext
    ) -> tuple[str, positive_pipeline.ManualSettings]:
        """An operator's manual exposure choice (`context.
        manual_positive_settings`, from the "Recadrage des positifs" screen)
        always wins over the campaign's own settings for this one
        regeneration — applies regardless of the campaign's configured mode."""
        if context.manual_positive_settings is not None:
            exposure_ev, contrast, shadows, highlights = context.manual_positive_settings
            return positive_pipeline.MODE_MANUAL, positive_pipeline.ManualSettings(
                exposure_ev=exposure_ev, contrast=contrast, shadows=shadows, highlights=highlights
            )
        manual = positive_cfg.manual_settings
        return positive_cfg.mode, positive_pipeline.ManualSettings(
            exposure_ev=manual.exposure_ev,
            contrast=manual.contrast,
            shadows=manual.shadows,
            highlights=manual.highlights,
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
