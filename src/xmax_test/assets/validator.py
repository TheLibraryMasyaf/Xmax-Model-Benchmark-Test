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

    def packet_timeline(self, path: Path) -> dict[str, float]:
        """Recover duration/FPS when a WebM container omits stream duration.

        MediaRecorder WebM output may have valid VP8 packets but no container
        duration or average frame rate.  Reading packet timestamps is slower
        than the normal probe, so it is deliberately used only as a fallback.
        """

        try:
            result = subprocess.run(
                [
                    self._binary,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_packets",
                    "-show_entries",
                    "packet=pts_time,dts_time,duration_time",
                    "-print_format",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(f"ffprobe packet timeline failed for {path}: {exc}") from exc
        if result.returncode != 0:
            raise ValidationError(
                f"ffprobe cannot read packet timeline for {path}: {result.stderr}"
            )
        import json

        try:
            packets = json.loads(result.stdout).get("packets", [])
        except (AttributeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"ffprobe produced invalid packet JSON for {path}") from exc
        timestamps: list[float] = []
        end_times: list[float] = []
        for packet in packets:
            timestamp = _number(packet.get("pts_time"))
            if timestamp is None:
                timestamp = _number(packet.get("dts_time"))
            if timestamp is None:
                continue
            timestamps.append(timestamp)
            packet_duration = _number(packet.get("duration_time")) or 0.0
            end_times.append(timestamp + max(0.0, packet_duration))
        if not timestamps:
            return {}
        start = min(timestamps)
        end = max(end_times or timestamps)
        duration = max(0.0, end - min(0.0, start))
        if duration <= 0:
            return {}
        return {
            "duration_s": round(duration, 3),
            "fps": round(len(timestamps) / duration, 3),
        }


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
        media = self._normalize(data, kind)
        if (
            (kind.endswith("_video") or kind == "result_video")
            and not media.get("duration_s")
        ):
            packet_timeline = getattr(self._probe, "packet_timeline", None)
            recovered = packet_timeline(path) if callable(packet_timeline) else {}
            if recovered.get("duration_s"):
                media["duration_s"] = recovered["duration_s"]
                media["fps"] = recovered.get("fps") or media.get("fps")
                media["duration_source"] = "packet_timeline"
        return media

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


def _number(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
