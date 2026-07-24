"""`ExportRunner` for the positive-finalize pass:
recomputes `JPEG_POSITIVE/<NAME><suffix>.jpg` with `imaging.print_engine`
(density-domain, calibrated for fidelity) after the quick capture-time
export already produced a first version with `imaging.positive`.

Deliberately a *separate* `ExportRunner`, not a branch inside
`MasterExportRunner`: this one holds no cached state on `self` between
calls (`imaging.print_engine.render_print` is a pure function of its
arguments), so — unlike `MasterExportRunner`, which caches the developed
master across a single image's three tasks and is documented as unsafe
under concurrent calls — instances of this runner may be called from
multiple worker threads at once. `core.queue.PooledExportExecutor` is
built for exactly this.

Ignores `ExportContext.content_frame_override`/`manual_positive_settings`
(the "Recadrage des positifs" screen's manual fields, `imaging.positive`'s
own parameter model — a different engine's parameter space). Does honor
`manual_print_*` (the print_engine calibration screen's own overrides)
and reports `PrintResult.flagged` back as
`ContentFrameOutcome.tonal_flagged`, same as `core.export_runner`'s own
print_engine path — an image finalized through this pool must classify
into deferred/applied/manual exactly the same way regardless of which of
the two passes actually rendered it.
"""

from __future__ import annotations

from pathlib import Path

from scanassistant.core.queue import (
    ContentFrameOutcome,
    ExportContext,
    ExportFailure,
    ExportResult,
    ExportTask,
)
from scanassistant.imaging import positive as positive_pipeline
from scanassistant.imaging import print_engine
from scanassistant.imaging.geometry import FrameGeometry
from scanassistant.imaging.raw import RawDecoder
from scanassistant.journal.journal import Journal
from scanassistant.metadata.writer import MetadataWriter, ProductionInfo
from scanassistant.project.campaign import Campaign
from scanassistant.project.layout import CampaignPaths


def _final_dimensions(values: list[int]) -> tuple[int, int]:
    width, height = values
    return (width, height)


class PositiveFinalizeRunner:
    """Production implementation, stateless across calls (see module docstring)."""

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

    def run(self, task: ExportTask) -> ExportResult | ExportFailure | None:
        context = task.context
        if context is None:
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
            path, outcome = self._render(task.name, context)
        except Exception as exc:  # unreadable/corrupt RAW, same catalog entry
            # as the quick pass (E-05) — a finalize failure never touches the
            # tier-1 file already on disk, so the image keeps a valid
            # positive either way.
            message = str(exc)
            self._journal.log(
                "SYSTEM",
                "error",
                image=task.name,
                level="error",
                details={"code": "E-05", "message": message, "stage": "positive_finalize"},
                result="error",
            )
            return ExportFailure(code="E-05", message=message)

        self._write_metadata(task.name, context, path)
        return ExportResult(content_frame=outcome)

    def _render(self, name: str, context: ExportContext) -> tuple[Path, ContentFrameOutcome]:
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
        )
        print_result = print_engine.render_print(
            self._decoder,
            context.raw_path,
            frame,
            rotation_deg=context.rotation_deg,
            size_mode=framing.size_mode,
            final_dimensions_px=_final_dimensions(framing.final_dimensions_px),
            user_wb=self._campaign.imaging.white_balance,
            overrides=overrides,
            horizontal_flip=positive_cfg.horizontal_flip,
        )
        path = self._paths.jpeg_positive_dir / f"{name}{positive_cfg.suffix}.jpg"
        positive_pipeline.write_jpeg_positive(
            print_result.pixels,
            path,
            quality=positive_cfg.quality,
            long_edge_px=positive_cfg.long_edge_px,
        )
        self._journal.log(
            "EXPORT",
            "jpeg_positive_finalize",
            image=name,
            details={
                "dmin": print_result.dmin,
                "dmax": print_result.dmax,
                "contrast": print_result.contrast,
                "exposure_shift": print_result.exposure_shift,
                "flagged": print_result.flagged,
                "content_mask_source": print_result.content_mask_source,
                "local_contrast_applied": print_result.local_contrast_applied,
            },
            result="ok",
        )
        x, y, w, h = print_result.content_frame
        support_height, support_width = print_result.support_shape
        support_area = support_width * support_height
        outcome = ContentFrameOutcome(
            x=x,
            y=y,
            width=w,
            height=h,
            fill=1.0,
            area_ratio=(w * h) / support_area if support_area > 0 else 0.0,
            source="manual" if print_result.content_mask_source == "manual" else "auto",
            tonal_flagged=print_result.flagged,
            fraction=(
                (x / support_width, y / support_height, w / support_width, h / support_height)
                if support_width > 0 and support_height > 0
                else None
            ),
        )
        return path, outcome

    def _write_metadata(self, name: str, context: ExportContext, derivative_path: Path) -> None:
        production = ProductionInfo(name=name, source_file=context.source_file)
        try:
            self._metadata_writer.write(
                context.raw_path, derivative_path, iptc=self._campaign.iptc, production=production
            )
        except Exception as exc:  # non-blocking by design, same as the master pipeline
            self._journal.log(
                "METADATA",
                "missing",
                image=name,
                level="warn",
                details={"reason": str(exc), "file": str(derivative_path)},
                result="error",
            )
