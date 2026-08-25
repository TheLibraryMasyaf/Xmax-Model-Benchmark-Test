"""Deterministic random Feed-frame capture for realtime touch recipes."""

from __future__ import annotations

import hashlib
import random
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ...errors import ContractError, ExternalServiceError, MissingDependencyError
from ...hashing import content_hash, file_sha256

SEEDED_RANDOM_SAFE_WINDOW_V1 = "seeded_random_safe_window_v1"


class SeededFrameCaptureExtractor:
    """Extract one reproducible random frame and cache it as an Artifact."""

    def __init__(self, artifacts: Any) -> None:
        self._artifacts = artifacts

    @staticmethod
    def select_timestamp(
        *,
        case_id: str,
        source_sha256: str,
        duration_s: float,
        policy: str,
    ) -> float:
        if policy != SEEDED_RANDOM_SAFE_WINDOW_V1:
            raise ContractError(f"unsupported realtime capture policy: {policy}")
        if duration_s <= 0:
            raise ContractError("realtime Feed capture requires a positive video duration")
        seed_text = f"{policy}:{case_id}:{source_sha256}"
        seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        # Avoid title/end cards while preserving a broad random window.
        start_s = duration_s * 0.1
        end_s = duration_s * 0.9
        return round(rng.uniform(start_s, max(start_s, end_s)), 3)

    def extract(
        self,
        *,
        case: dict[str, Any],
        source_asset: dict[str, Any],
        source_path: Path,
        policy: str,
    ) -> dict[str, Any]:
        if source_asset.get("kind") != "feed_video":
            raise ContractError(
                "realtime feed_capture input requires a feed_video source; "
                f"got {source_asset.get('kind')!r}"
            )
        source_sha256 = str(source_asset.get("sha256") or file_sha256(source_path))
        duration_s = float(source_asset.get("media", {}).get("duration_s") or 0)
        timestamp_s = self.select_timestamp(
            case_id=case["case_id"],
            source_sha256=source_sha256,
            duration_s=duration_s,
            policy=policy,
        )
        capture_key = content_hash(
            {
                "case_id": case["case_id"],
                "source_asset_id": source_asset["asset_id"],
                "source_sha256": source_sha256,
                "timestamp_s": timestamp_s,
                "policy": policy,
                "producer_version": "realtime-feed-capture-v1",
            }
        )[:24]
        relative_path = f"realtime/{source_asset['asset_id']}/{capture_key}.jpg"
        uri = self._artifacts.uri("captures", relative_path)
        target = self._artifacts.resolve(uri)
        if not target.is_file() or target.stat().st_size == 0:
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg is None:
                raise MissingDependencyError(
                    "ffmpeg is required to extract a realtime Feed capture"
                )
            with tempfile.TemporaryDirectory(prefix="xmax-realtime-capture-") as directory:
                temporary = Path(directory) / "capture.jpg"
                try:
                    completed = subprocess.run(
                        [
                            ffmpeg,
                            "-y",
                            "-v",
                            "error",
                            "-i",
                            str(source_path),
                            "-ss",
                            f"{timestamp_s:.3f}",
                            "-frames:v",
                            "1",
                            "-q:v",
                            "2",
                            str(temporary),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ExternalServiceError(
                        f"realtime Feed capture extraction failed: {exc}"
                    ) from exc
                if completed.returncode != 0 or not temporary.is_file():
                    raise ExternalServiceError(
                        "realtime Feed capture extraction failed for "
                        f"{source_asset['asset_id']}: {completed.stderr[-500:]}"
                    )
                self._artifacts.put_file("captures", temporary, relative_path)
        return {
            "uri": uri,
            "sha256": file_sha256(target),
            "bytes": target.stat().st_size,
            "source_asset_id": source_asset["asset_id"],
            "source_sha256": source_sha256,
            "timestamp_s": timestamp_s,
            "capture_policy": policy,
            "producer_version": "realtime-feed-capture-v1",
        }
