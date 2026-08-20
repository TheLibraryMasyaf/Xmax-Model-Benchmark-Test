"""Import the minimal human supervision contract: one video + one comment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..assets.models import DownloadResult
from ..assets.registry import AssetRegistry
from ..errors import ContractError, ValidationError
from ..evaluation.frames import FfmpegFrameExtractor
from ..evaluation.sampling import global_samples
from ..hashing import file_sha256
from .importer import HumanSignalImporter


class HumanMediaImporter:
    def __init__(
        self,
        repository: Any,
        artifacts: Any,
        *,
        frame_count: int = 8,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._frame_count = frame_count
        self._asset_registry = AssetRegistry(repository, artifacts, clock=clock)
        self._frames = FfmpegFrameExtractor(artifacts)
        self._signals = HumanSignalImporter(repository, clock=clock)

    def import_records(
        self, records: list[dict[str, Any]], *, source_file: str = "inline-media"
    ) -> dict[str, Any]:
        prepared = []
        errors = []
        for index, record in enumerate(records):
            try:
                prepared.append(self._prepare(record))
            except Exception as exc:
                errors.append(
                    {
                        "code": "xmax.human_media_import_failed",
                        "message": f"record {index}: {exc}",
                        "stage": "feedback",
                        "retryable": False,
                    }
                )
        outcome = self._signals.import_records(prepared, source_file=source_file)
        return {
            **outcome,
            "errors": errors + outcome["errors"],
            "prepared": len(prepared),
        }

    def _prepare(self, record: dict[str, Any]) -> dict[str, Any]:
        raw_text = record.get("raw_text") or record.get("comment")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ContractError("human media sample requires a non-empty comment")
        video_path = record.get("video_path") or record.get("video")
        if not isinstance(video_path, str) or not video_path:
            raise ContractError("human media sample requires video_path")
        path = Path(video_path).expanduser().resolve()
        if not path.is_file():
            raise ValidationError(f"human video missing: {path}")
        signal_id = record.get("signal_id")
        if not signal_id:
            raise ContractError("human media sample requires stable signal_id")
        sha256 = file_sha256(path)
        asset = self._asset_registry.register_download(
            {
                "source_id": record.get("source_id", "human-media"),
                "kind": "human_media",
                "asset_kind": "result_video",
            },
            DownloadResult(
                remote_key=record.get("source_record_id", signal_id),
                path=path,
                sha256=sha256,
                bytes=path.stat().st_size,
                attachment_token=record.get("attachment_token"),
                metadata={
                    "kind": "result_video",
                    "record_id": record.get("source_record_id"),
                    "source_group_id": record.get("source_group_id"),
                    "human_signal_id": signal_id,
                    "source_field": record.get("source_field"),
                },
            ),
        )
        duration_s = float(asset.get("media", {}).get("duration_s") or 0)
        if duration_s <= 0:
            raise ValidationError(f"human video has no decodable duration: {path}")
        fps = float(asset.get("media", {}).get("fps") or 0)
        # Container duration points immediately after the final decodable
        # frame for many MP4 files. Never seek the exact EOF timestamp.
        frame_margin = max(0.05, (1.0 / fps) if fps > 0 else 0.05)
        sample_duration = max(0.001, duration_s - frame_margin)
        timestamps = global_samples(
            sample_duration, self._frame_count, cover_edges=True
        )
        frames = self._frames.extract_asset(
            signal_id, asset, timestamps, "human_result"
        )
        image_paths = [
            str(self._artifacts.resolve(frame["uri"]).resolve()) for frame in frames
        ]
        return {
            **record,
            "signal_id": signal_id,
            "sample_id": record.get("sample_id") or signal_id,
            "raw_text": raw_text.strip(),
            "source_type": record.get("source_type", "independent_human_eval"),
            "review_context": record.get("review_context", "unknown"),
            "result_asset_id": asset["asset_id"],
            "evidence_images": image_paths,
            "annotations": {
                **record.get("annotations", {}),
                "sampled_timestamps_s": [frame["timestamp_s"] for frame in frames],
                "frame_uris": [frame["uri"] for frame in frames],
            },
        }
