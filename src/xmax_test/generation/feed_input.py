"""Versioned removal of the first decoded Feed frame, before role binding."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from ..hashing import content_hash, file_sha256

FEED_INPUT_POLICY = "drop-first-decoded-frame-v1"


class FfmpegFeedPreprocessor:
    """Keep originals immutable; share verified output across both providers.

    The cutoff is frame 1's presentation timestamp, not 1 / average FPS.
    Audio uses the same cutoff and timestamp origin, preserving A/V offsets.
    Output-hash receipts prevent repeated trimming after local re-ingestion.
    """

    def __init__(self, artifacts: Any, validator: Any, *, runner: Any = subprocess.run):
        self.artifacts = artifacts
        self.validator = validator
        self.runner = runner

    def _run(self, args: list[str], timeout: int = 1800) -> Any:
        try:
            result = self.runner(args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(f"Feed preprocessing failed: {exc}") from exc
        if result.returncode:
            raise ValidationError(f"Feed preprocessing failed: {result.stderr[-500:]}")
        return result

    def _frames(self, path: Path) -> tuple[list[float], float]:
        result = self._run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_frames", "-show_entries", "frame=best_effort_timestamp_time,duration_time,pkt_duration_time",
            "-of", "json", str(path),
        ])
        try:
            facts = json.loads(result.stdout)["frames"]
            times = [float(f["best_effort_timestamp_time"]) for f in facts]
            tail = float(facts[-1].get("duration_time") or facts[-1].get("pkt_duration_time") or 0)
            if tail <= 0 and len(times) > 1:
                tail = times[-1] - times[-2]
            return times, tail
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise ValidationError("Feed frame timestamps are missing or invalid") from exc

    def prepare(self, asset: dict[str, Any]) -> dict[str, Any]:
        if asset.get("kind") != "feed_video":
            return asset
        source = Path(asset["path"])
        source_sha = file_sha256(source)
        if asset.get("sha256") and asset["sha256"] != source_sha:
            raise ValidationError("Feed source hash changed before preprocessing")
        receipt_path = self.artifacts.resolve(
            f"artifact://feed-preprocessing/by-output/{source_sha}.json"
        )
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt["policy"] != FEED_INPUT_POLICY or receipt["output_sha256"] != source_sha:
                raise ValidationError("Feed preprocessing receipt mismatch")
            return {**asset, "feed_preprocessing": receipt}
        cache_key = content_hash({"policy": FEED_INPUT_POLICY, "source_sha256": source_sha})
        target = self.artifacts.resolve(f"artifact://feed-preprocessing/{cache_key}/feed.mp4")
        manifest = target.with_suffix(".json")
        if manifest.exists():
            receipt = json.loads(manifest.read_text())
            if (receipt["source_sha256"] != source_sha or receipt["policy"] != FEED_INPUT_POLICY
                    or not target.is_file() or file_sha256(target) != receipt["output_sha256"]):
                raise ValidationError("Cached trimmed Feed failed integrity verification")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Decode every frame before permitting any paid request.
            self._run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(source), "-f", "null", "-"])
            frames, tail_duration = self._frames(source)
            if len(frames) < 2 or frames[1] <= frames[0]:
                raise ValidationError("Feed needs at least two frames with increasing timestamps")
            cutoff = frames[1]
            temp = target.with_name(f".{uuid.uuid4().hex}.mp4")
            command = [
                "ffmpeg", "-nostdin", "-y", "-v", "error", "-xerror", "-copyts", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a?",
                "-vf", f"trim=start_frame=1,setpts=PTS-({cutoff:.9f})/TB",
                "-af", f"atrim=start={cutoff:.9f},asetpts=PTS-({cutoff:.9f})/TB",
                "-fps_mode", "passthrough", "-enc_time_base", "demux",
                "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                # FFmpeg 7 can lose the final packet duration after trim. With
                # no B-frame reordering, preserve the final display interval
                # explicitly without changing any frame's PTS or duplicating it.
                "-bf", "0", "-bsf:v", f"setts=duration=if(eq(N\\,{len(frames)-2})\\,{tail_duration:.9f}/TB\\,DURATION)",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(temp),
            ]
            try:
                self._run(command)
                self._run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(temp), "-f", "null", "-"])
                output_frames, _ = self._frames(temp)
                if len(output_frames) != len(frames) - 1:
                    raise ValidationError("Feed preprocessing did not remove exactly one frame")
                if any(abs(actual - (original - cutoff)) > .002
                       for original, actual in zip(frames[1:], output_frames)):
                    raise ValidationError("Feed preprocessing changed the remaining frame timeline")
                media = self.validator.validate(temp, "feed_video")
                original_media = self.validator.validate(source, "feed_video")
                if any(media.get(k) != original_media.get(k) for k in ["width", "height", "has_audio"]):
                    raise ValidationError("Feed preprocessing changed dimensions or audio presence")
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
            receipt = {
                "policy": FEED_INPUT_POLICY, "source_asset_id": asset.get("asset_id"),
                "source_sha256": source_sha, "output_sha256": file_sha256(target),
                "cutoff_timestamp_s": cutoff, "removed_duration_s": cutoff - frames[0],
                "source_frames": len(frames), "output_frames": len(output_frames),
                "media": media, "command": [str(target) if x == str(temp) else x for x in command],
            }
            manifest.write_text(json.dumps(receipt, indent=2))
        output_receipt = self.artifacts.resolve(
            f"artifact://feed-preprocessing/by-output/{receipt['output_sha256']}.json"
        )
        output_receipt.parent.mkdir(parents=True, exist_ok=True)
        output_receipt.write_text(json.dumps(receipt, indent=2))
        return {**asset, "path": str(target), "sha256": receipt["output_sha256"],
                "mime_type": "video/mp4", "media": receipt["media"], "feed_preprocessing": receipt}


class FakeFeedPreprocessor:
    """Explicit no-media test double; never used by production composition."""

    def prepare(self, asset: dict[str, Any]) -> dict[str, Any]:
        return asset
