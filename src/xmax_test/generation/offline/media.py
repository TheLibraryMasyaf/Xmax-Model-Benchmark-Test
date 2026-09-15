"""Provider-specific, deterministic normalization for offline video inputs."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from ...errors import ContractError, ValidationError
from ...hashing import content_hash, file_sha256


DECART_MAX_INPUT_BYTES = 200_000_000
DECART_TARGET_BYTES = 180_000_000


class FfmpegOfflineInputNormalizer:
    """Create Lucy-compatible MP4/H.264 inputs without mutating source assets."""

    PROFILE = "decart-720p-h264-pad-v1"
    LONG_VIDEO_PROFILE = "decart-720p-h264-size-capped-v2"
    CRF_CAPPED_PROFILE = "decart-720p-h264-crf18-capped-v3"

    def __init__(
        self,
        artifacts: Any,
        validator: Any,
        *,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._artifacts = artifacts
        self._validator = validator
        self._runner = runner

    def normalize(self, asset: dict[str, Any], profile: str) -> dict[str, Any]:
        if profile not in {self.PROFILE, self.LONG_VIDEO_PROFILE, self.CRF_CAPPED_PROFILE}:
            raise ContractError(f"unsupported offline input normalization profile: {profile}")
        source = Path(asset["path"])
        if not source.is_file():
            raise ValidationError(f"normalization source missing: {source}")
        source_sha = asset.get("sha256") or file_sha256(source)
        cache_key = content_hash({"profile": profile, "source_sha256": source_sha})
        target = self._artifacts.resolve(
            f"artifact://normalized/{cache_key}/input.mp4"
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        media = dict(asset.get("media") or {})
        width = int(media.get("width") or 0)
        height = int(media.get("height") or 0)
        if width <= 0 or height <= 0:
            media = self._validator.validate(source, str(asset.get("kind") or "feed_video"))
            width = int(media.get("width") or 0)
            height = int(media.get("height") or 0)
        out_width, out_height = (1280, 720) if width >= height else (720, 1280)

        vf = (
            f"scale={out_width}:{out_height}:force_original_aspect_ratio=decrease,"
            f"pad={out_width}:{out_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
        command = [
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?", "-vf", vf,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "medium",
        ]
        if profile in {self.LONG_VIDEO_PROFILE, self.CRF_CAPPED_PROFILE}:
            duration_s = float(media.get("duration_s") or 0.0)
            if duration_s <= 0:
                raise ValidationError(
                    "Lucy long-video normalization requires a positive source duration"
                )
            # Reserve room for the MP4 container and AAC. Capping maxrate
            # makes the requested bound meaningful; the post-encode size
            # check below remains the final fail-closed guard.
            audio_kbps = 128 if media.get("has_audio") else 0
            video_kbps = max(
                250,
                int((DECART_TARGET_BYTES * 8 / duration_s / 1000) - audio_kbps),
            )
            video_kbps = min(video_kbps, 8_000 if profile == self.CRF_CAPPED_PROFILE else 20_000)
            if profile == self.LONG_VIDEO_PROFILE:
                command.extend([
                    "-b:v", f"{video_kbps}k",
                    "-maxrate", f"{video_kbps}k",
                    "-bufsize", f"{video_kbps * 2}k",
                ])
            else:
                command.extend([
                    "-crf", "18",
                    "-maxrate", f"{video_kbps}k",
                    "-bufsize", f"{video_kbps * 2}k",
                ])
        else:
            command.extend(["-crf", "18"])
        command.extend([
            "-c:a", "aac", "-b:a", "128k" if profile in {self.LONG_VIDEO_PROFILE, self.CRF_CAPPED_PROFILE} else "192k",
            "-movflags", "+faststart", "__OUTPUT__",
        ])
        recorded_command = [
            str(target) if item == "__OUTPUT__" else item for item in command
        ]

        if not target.is_file() or target.stat().st_size == 0:
            temp = target.with_name(f".{target.stem}-{uuid.uuid4().hex}.tmp.mp4")
            command[-1] = str(temp)
            try:
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=max(600, int(float(media.get("duration_s") or 0) * 20)),
                )
                if completed.returncode != 0 or not temp.is_file() or temp.stat().st_size == 0:
                    raise ValidationError(
                        f"ffmpeg Lucy input normalization failed for {asset['asset_id']}: "
                        f"{str(completed.stderr)[-500:]}"
                    )
                if profile in {self.LONG_VIDEO_PROFILE, self.CRF_CAPPED_PROFILE} and temp.stat().st_size >= DECART_MAX_INPUT_BYTES:
                    raise ValidationError(
                        f"Lucy normalized input is not below {DECART_MAX_INPUT_BYTES} bytes: "
                        f"{temp.stat().st_size} bytes"
                    )
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)

        if profile in {self.LONG_VIDEO_PROFILE, self.CRF_CAPPED_PROFILE} and target.stat().st_size >= DECART_MAX_INPUT_BYTES:
            raise ValidationError(
                f"Lucy normalized input is not below {DECART_MAX_INPUT_BYTES} bytes: "
                f"{target.stat().st_size} bytes"
            )

        normalized_media = self._validator.validate(target, "feed_video")
        if normalized_media.get("video_codec") != "h264":
            raise ValidationError("Lucy input normalization did not produce H.264")
        if (normalized_media.get("width"), normalized_media.get("height")) not in {
            (1280, 720),
            (720, 1280),
        }:
            raise ValidationError("Lucy input normalization did not produce a 720p canvas")
        if profile in {self.LONG_VIDEO_PROFILE, self.CRF_CAPPED_PROFILE}:
            source_duration = float(media.get("duration_s") or 0.0)
            output_duration = float(normalized_media.get("duration_s") or 0.0)
            if source_duration > 0 and abs(output_duration - source_duration) > 0.25:
                raise ValidationError(
                    "Lucy long-video normalization changed the source duration"
                )
            if bool(normalized_media.get("has_audio")) != bool(media.get("has_audio")):
                raise ValidationError(
                    "Lucy long-video normalization changed the source audio presence"
                )
        return {
            **asset,
            "asset_id": f"normalized:{cache_key[:16]}",
            "path": str(target),
            "sha256": file_sha256(target),
            "mime_type": "video/mp4",
            "media": normalized_media,
            "normalization": {
                "profile": profile,
                "source_asset_id": asset.get("asset_id"),
                "source_sha256": source_sha,
                "input_sha256": source_sha,
                "command": recorded_command,
                "max_bytes": DECART_MAX_INPUT_BYTES if profile in {self.LONG_VIDEO_PROFILE, self.CRF_CAPPED_PROFILE} else None,
            },
        }
