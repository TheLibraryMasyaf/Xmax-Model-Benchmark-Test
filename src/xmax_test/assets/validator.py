"""Media validation for downloaded assets.

A successful download exit code is not enough: the media must decode. Video
checks use ffprobe when available; a pluggable probe keeps tests offline and
deterministic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Protocol

from ..errors import ValidationError

MAGIC: dict[bytes, str] = {
    b"\x00\x00\x00\x18ftyp": "video/mp4",
    b"\x1aE\xdf\xa3": "video/webm",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


class MediaProbe(Protocol):
    def probe(self, path: Path) -> dict[str, Any]:
        """Return media facts; raise on undecodable content."""


class FfprobeMediaProbe:
    def __init__(self, ffprobe_binary: str = "ffprobe") -> None:
        self._binary = ffprobe_binary

    def probe(self, path: Path) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [
                    self._binary,
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(f"ffprobe failed for {path}: {exc}") from exc
        if result.returncode != 0:
            raise ValidationError(f"ffprobe cannot decode {path}: {result.stderr}")
        import json

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"ffprobe produced invalid JSON for {path}") from exc
        return data


class MediaValidator:
    """Validates media and produces Asset-ready metadata."""

    def __init__(self, probe: MediaProbe | None = None) -> None:
        self._probe = probe or FfprobeMediaProbe()

    def validate(self, path: Path, kind: str) -> dict[str, Any]:
        """Validate media; return normalized media facts or raise.

        Returns a dict with at least ``width``/``height`` for images and
        ``duration_s``/``fps``/``has_audio``/stream info for videos.
        """

        if not path.is_file() or path.stat().st_size == 0:
            raise ValidationError(f"media file missing or empty: {path}")
        try:
            data = self._probe.probe(path)
        except ValidationError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise ValidationError(f"media probe error for {path}: {exc}") from exc
        return self._normalize(data, kind)

    def probe_sync(self, path: Path) -> dict[str, Any]:
        """Backward-compatible alias used by older call sites."""

        return self.validate(path, "feed_video")

    @staticmethod
    def _normalize(data: dict[str, Any], kind: str) -> dict[str, Any]:
        streams = data.get("streams", []) if isinstance(data, dict) else []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = data.get("format", {}) if isinstance(data, dict) else {}
        if video is None and (kind.endswith("_video") or kind == "result_video"):
            raise ValidationError(f"{kind} has no decodable video stream")
        media: dict[str, Any] = {"format": fmt.get("format_name", "")}
        if video is not None:
            media["width"] = video.get("width")
            media["height"] = video.get("height")
            fps = _fps(video.get("avg_frame_rate"))
            media["fps"] = fps
            duration = _duration(fmt.get("duration")) or _duration(video.get("duration"))
            media["duration_s"] = duration
            media["video_codec"] = video.get("codec_name")
        if audio is not None:
            media["has_audio"] = True
            media["audio_sample_rate"] = audio.get("sample_rate")
            media["audio_duration_s"] = _duration(audio.get("duration"))
        else:
            media["has_audio"] = False
        if kind.endswith("_image") and video is not None:
            media["image_width"] = video.get("width")
            media["image_height"] = video.get("height")
        return media


def sniff_mime(path: Path) -> str | None:
    """Cheap MIME sniff from file signature; None when unknown."""

    with path.open("rb") as handle:
        head = handle.read(16)
    for signature, mime in MAGIC.items():
        if head.startswith(signature):
            return mime
    return None


def _fps(value: Any) -> float | None:
    if not value or "/" not in str(value):
        return None
    try:
        numerator, denominator = str(value).split("/", 1)
        if int(denominator) == 0:
            return None
        return round(int(numerator) / int(denominator), 3)
    except (ValueError, ZeroDivisionError):
        return None


def _duration(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None
