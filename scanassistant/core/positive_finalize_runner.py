"""`ExportRunner` for the positive-finalize pass (DECISIONS.md I-179):
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
own parameter model): the calibration screen for this new engine
(specifications/13_INVERSION_NEGATIFS.md §9) has its own override
mechanism, not yet wired in — until it is, this runner only ever produces
the fully-automatic render.
"""

from __future__ import annotations

from pathlib import Path

from scanassistant.core.queue import ExportContext, ExportFailure, ExportResult, ExportTask
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
            result = self._render(task.name, context)
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

        self._write_metadata(task.name, context, result)
        return ExportResult()

    def _render(self, name: str, context: ExportContext) -> Path:
        positive_cfg = self._campaign.exports.jpeg_positive
        framing = self._campaign.framing
        frame = FrameGeometry(
            x=context.x,
            y=context.y,
            width=context.width,
            height=context.height,
            angle_deg=context.angle_deg,
        )
        print_result = print_engine.render_print(
            self._decoder,
            context.raw_path,
            frame,
            rotation_deg=context.rotation_deg,
            size_mode=framing.size_mode,
            final_dimensions_px=_final_dimensions(framing.final_dimensions_px),
            user_wb=self._campaign.imaging.white_balance,
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
        return path

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
