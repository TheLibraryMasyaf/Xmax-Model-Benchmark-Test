"""Media preprocess service.

Builds global frames, event windows, ROIs and contact sheets for one
GenerationRun. Outputs are versioned and cached by
``input_hash + config_hash + producer_version``; undecodable media never
produces fabricated evidence.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import ContractError, ValidationError
from ..hashing import content_hash
from ..time import utc_now
from .contact_sheet import ContactSheetBuilder
from .roi import RoiExtractor
from .sampling import global_samples, local_high_fps_timestamps

PROCESSOR_VERSION = "0.1.0"


class PreprocessService:
    def __init__(
        self,
        repository: Any,
        artifacts: Any,
        *,
        frame_extractor: Any = None,
        contact_sheet: ContactSheetBuilder | None = None,
        roi: RoiExtractor | None = None,
        global_frame_count: int = 8,
        window_before_s: float = 1.0,
        window_after_s: float = 2.0,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._frame_extractor = frame_extractor
        self._contact_sheet = contact_sheet or ContactSheetBuilder()
        self._roi = roi or RoiExtractor()
        self._global_frame_count = global_frame_count
        self._window_before_s = window_before_s
        self._window_after_s = window_after_s
        self._clock = clock

    def build(self, run: dict[str, Any]) -> dict[str, Any]:
        """Build preprocess artifacts for one completed run."""

        if run.get("status") != "completed":
            raise ContractError(f"preprocess requires a completed run, got {run.get('status')}")
        result_asset_id = run.get("result_asset_id")
        if not result_asset_id:
            raise ContractError(f"run {run['run_id']} has no result asset")

        asset = self._repository.get_asset(result_asset_id)
        try:
            case = self._repository.get_test_case(run["case_id"])
        except Exception:
            case = {}
        duration_s = float(asset.get("media", {}).get("duration_s") or 0)
        if duration_s <= 0:
            raise ValidationError(
                f"run {run['run_id']} result media has no duration; cannot sample"
            )

        parameters = {
            "global_frame_count": self._global_frame_count,
            "window_before_s": self._window_before_s,
            "window_after_s": self._window_after_s,
            "producer_version": PROCESSOR_VERSION,
        }
        config_hash = content_hash(parameters)
        input_hash = content_hash(
            {
                "run": run["run_id"],
                "result_asset": result_asset_id,
                "media": asset.get("media"),
                "feed_asset_id": case.get("feed_asset_id"),
                "prompt_asset_ids": case.get("prompt_asset_ids", []),
            }
        )

        existing = self._repository.find_preprocess_run(input_hash, config_hash, PROCESSOR_VERSION)
        if existing is not None:
            return existing

        events = self._repository.get_event_log(run["run_id"])
        event_times = [
            float(item.get("payload", {}).get("ts", 0))
            for item in events
            if item.get("payload", {}).get("ts") is not None
        ]

        global_ts = global_samples(duration_s, self._global_frame_count, cover_edges=True)
        window_frames: list[dict[str, Any]] = []
        for event_time in event_times:
            window_frames.extend(
                local_high_fps_timestamps(
                    event_time,
                    duration_s,
                    before_s=self._window_before_s,
                    after_s=self._window_after_s,
                )
            )

        frames = self._extract_frames(run, asset, global_ts, window_frames)
        if not frames:
            raise ValidationError(
                f"run {run['run_id']} media could not be decoded; no evidence produced"
            )

        global_sheet = self._contact_sheet.build(
            run_id=run["run_id"],
            frames=frames["global"],
            kind="global",
            producer_version=PROCESSOR_VERSION,
            parameters=parameters,
        )
        event_sheets = [
            self._contact_sheet.build(
                run_id=run["run_id"],
                frames=frames["windows"][index : index + 1],
                kind="event_window",
                producer_version=PROCESSOR_VERSION,
                parameters=parameters,
            )
            for index in range(len(frames["windows"]))
        ]
        evidence_groups: list[dict[str, Any]] = []
        input_sheets: list[dict[str, Any]] = []
        input_assets: list[tuple[str, str]] = []
        if case.get("feed_asset_id"):
            input_assets.append(("feed", case["feed_asset_id"]))
        input_assets.extend(
            (f"prompt_reference_{index + 1}", asset_id)
            for index, asset_id in enumerate(case.get("prompt_asset_ids", []))
        )
        seen_assets = {result_asset_id}
        for role, asset_id in input_assets:
            if asset_id in seen_assets:
                continue
            seen_assets.add(asset_id)
            try:
                input_asset = self._repository.get_asset(asset_id)
                input_frames = self._extract_input_asset(run, input_asset, role)
            except Exception:
                input_frames = []
            if not input_frames:
                continue
            evidence_groups.append({"role": role, "asset_id": asset_id, "frames": input_frames})
            input_sheets.append(
                self._contact_sheet.build(
                    run_id=run["run_id"],
                    frames=input_frames,
                    kind=role,
                    producer_version=PROCESSOR_VERSION,
                    parameters=parameters,
                )
            )
        evidence_groups.append(
            {
                "role": "result_video",
                "asset_id": result_asset_id,
                "frames": frames["global"],
            }
        )
        rois = self._roi.extract({"index": 0, "run_id": run["run_id"]})

        preprocess = {
            "preprocess_id": f"prep-{input_hash[:12]}",
            "run_id": run["run_id"],
            "input_hash": input_hash,
            "config_hash": config_hash,
            "producer_version": PROCESSOR_VERSION,
            "status": "completed",
            "parameters": parameters,
            "global_timestamps": global_ts,
            "event_timestamps": event_times,
            "window_timestamps": window_frames,
            "frame_counts": {
                "global": len(frames["global"]),
                "event_windows": len(frames["windows"]),
            },
            "sheets": input_sheets + [global_sheet] + event_sheets,
            "evidence_groups": evidence_groups,
            "rois": rois,
            "created_at": utc_now(),
        }
        uri = self._artifacts.put_bytes(
            "preprocessing",
            f"{preprocess['preprocess_id']}/manifest.json",
            json.dumps(preprocess, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        preprocess["manifest_uri"] = uri
        self._repository.upsert_preprocess_run(preprocess)
        return preprocess

    def _extract_frames(
        self,
        run: dict[str, Any],
        asset: dict[str, Any],
        global_ts: list[float],
        window_ts: list[float],
    ) -> dict[str, Any]:
        if self._frame_extractor is None:
            # No real extractor: emit structural frame records only if the
            # media facts are decodable (non-empty media dict).
            if not asset.get("media"):
                return {"global": [], "windows": []}
            globals_frames = [
                {"index": index, "timestamp_s": ts, "source": "global"}
                for index, ts in enumerate(global_ts)
            ]
            window_frames = [
                {"index": 100 + index, "timestamp_s": ts, "source": "window"}
                for index, ts in enumerate(window_ts)
            ]
            return {"global": globals_frames, "windows": window_frames}
        return self._frame_extractor.extract(run, asset, global_ts, window_ts)

    def _extract_input_asset(
        self, run: dict[str, Any], asset: dict[str, Any], role: str
    ) -> list[dict[str, Any]]:
        duration_s = float(asset.get("media", {}).get("duration_s") or 1.0)
        timestamps = global_samples(duration_s, min(3, self._global_frame_count), cover_edges=True)
        if self._frame_extractor is not None and hasattr(self._frame_extractor, "extract_asset"):
            return self._frame_extractor.extract_asset(run["run_id"], asset, timestamps, role)
        if not asset.get("media"):
            return []
        return [
            {"index": 20000 + index, "timestamp_s": ts, "source": role}
            for index, ts in enumerate(timestamps)
        ]
