"""Real ffmpeg frame extraction for evaluation evidence."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..errors import MissingDependencyError, ValidationError
from ..hashing import file_sha256


class FfmpegProviderMediaNormalizer:
    """Create a provider-compatible MP4 while preserving the source WebM."""

    version = "webm-h264-v2"

    def __init__(self, artifacts: Any, binary: str = "ffmpeg", timeout_s: int = 180) -> None:
        self._artifacts = artifacts
        self._binary = binary
        self._timeout = timeout_s

    def normalize(self, run_id: str, asset: dict[str, Any]) -> dict[str, Any] | None:
        uri = str(asset.get("uri") or "")
        mime = str(asset.get("mime_type") or "")
        if not (uri.lower().endswith(".webm") or mime == "video/webm"):
            return None
        source = self._artifacts.resolve(uri)
        if not source.is_file():
            raise ValidationError(f"provider media source is missing: {uri}")
        source_hash = str(asset.get("sha256") or file_sha256(source))
        relative = f"{run_id}/provider-media/{self.version}/{source_hash[:16]}.mp4"
        target_uri = self._artifacts.uri("preprocessing", relative)
        target = self._artifacts.resolve(target_uri)
        if not target.is_file():
            with tempfile.TemporaryDirectory(prefix="xmax-provider-media-") as directory:
                output = Path(directory) / "result.mp4"
                try:
                    result = subprocess.run(
                        [
                            self._binary,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-i",
                            str(source),
                            "-map",
                            "0:v:0",
                            "-an",
                            "-vf",
                            "fps=24",
                            "-r",
                            "24",
                            "-c:v",
                            "libx264",
                            "-preset",
                            "veryfast",
                            "-crf",
                            "28",
                            "-pix_fmt",
                            "yuv420p",
                            "-movflags",
                            "+faststart",
                            "-y",
                            str(output),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=self._timeout,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ValidationError(f"provider media normalization failed: {exc}") from exc
                if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                    raise ValidationError(
                        "provider media normalization produced no MP4: "
                        f"{result.stderr[-500:]}"
                    )
                self._artifacts.put_file("preprocessing", output, relative)
        return {
            "uri": target_uri,
            "mime_type": "video/mp4",
            "bytes": target.stat().st_size,
            "source_asset_id": asset.get("asset_id"),
            "source_sha256": source_hash,
            "normalizer_version": self.version,
        }


class FfmpegFrameExtractor:
    """Extract timestamped JPEG evidence and store it in ArtifactStore."""

    def __init__(self, artifacts: Any, binary: str = "ffmpeg", timeout_s: int = 120) -> None:
        self._artifacts = artifacts
        self._binary = binary
        self._timeout = timeout_s

    def extract(
        self,
        run: dict[str, Any],
        asset: dict[str, Any],
        global_ts: list[float],
        window_ts: list[float],
    ) -> dict[str, list[dict[str, Any]]]:
        from shutil import which

        if which(self._binary) is None:
            raise MissingDependencyError(
                f"real preprocessing requires ffmpeg on PATH: {self._binary}"
            )
        source = self._artifacts.resolve(asset["uri"])
        if not source.is_file():
            raise ValidationError(f"result video artifact is missing: {asset['uri']}")
        return {
            "global": self._extract_group(run["run_id"], source, "global", global_ts, 0),
            "windows": self._extract_group(run["run_id"], source, "window", window_ts, 10000),
        }

    def extract_asset(
        self,
        run_id: str,
        asset: dict[str, Any],
        timestamps: list[float],
        role: str,
    ) -> list[dict[str, Any]]:
        source = self._artifacts.resolve(asset["uri"])
        if not source.is_file():
            raise ValidationError(f"evidence asset is missing: {asset['uri']}")
        return self._extract_group(run_id, source, role, timestamps, 20000)

    def _extract_group(
        self, run_id: str, source: Path, kind: str, timestamps: list[float], offset: int
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="xmax-frames-") as directory:
            temp_root = Path(directory)
            for position, timestamp in enumerate(timestamps):
                output = temp_root / f"{kind}-{position:05d}.jpg"
                result = None
                actual_timestamp = max(0.0, timestamp)
                for retreat_s in (0.0, 0.1, 0.25, 0.5):
                    actual_timestamp = max(0.0, timestamp - retreat_s)
                    command = [
                        self._binary,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        f"{actual_timestamp:.3f}",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        "-y",
                        str(output),
                    ]
                    try:
                        result = subprocess.run(
                            command,
                            capture_output=True,
                            text=True,
                            timeout=self._timeout,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise ValidationError(f"ffmpeg frame extraction failed: {exc}") from exc
                    if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
                        break
                if (
                    result is None
                    or result.returncode != 0
                    or not output.is_file()
                    or output.stat().st_size == 0
                ):
                    raise ValidationError(
                        f"ffmpeg cannot decode {source.name} at {timestamp:.3f}s: "
                        f"{(result.stderr if result else '')[-500:]}"
                    )
                stored = self._artifacts.put_file(
                    "preprocessing",
                    output,
                    f"{run_id}/frames/{kind}-{position:05d}.jpg",
                )
                frames.append(
                    {
                        "index": offset + position,
                        "timestamp_s": round(actual_timestamp, 3),
                        "source": kind,
                        "uri": stored["uri"],
                        "sha256": stored["sha256"],
                    }
                )
        return frames
