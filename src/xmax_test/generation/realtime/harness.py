"""Subprocess bridge to the browser-based realtime harness."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ...errors import ExternalServiceError, MissingDependencyError

# Chrome/WebCodecs can only decode H.264/AAC MP4; HEVC inputs (the default
# export codec for edited XMAX feeds) must be transcoded before the browser
# SDK can open a video track from them.
_H264_TRANSCODE_DIR = "var/realtime-transcodes"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BrowserRealtimeHarness:
    def __init__(
        self,
        project_root: Path,
        artifacts: Any,
        repository: Any,
        *,
        api_key: str | None = None,
        timeout_s: int = 900,
        headed: bool = False,
    ) -> None:
        self._root = Path(project_root)
        self._artifacts = artifacts
        self._repository = repository
        self._timeout = timeout_s
        self._headed = headed
        self._api_key = api_key

    @staticmethod
    def _video_codec(path: Path) -> str | None:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return None
        try:
            completed = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "csv=p=0",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None

    def _ensure_browser_compatible(
        self, input_path: Path, sha256: str
    ) -> tuple[Path, bool]:
        """Return a browser-decodable copy of the input video.

        HEVC/H.265 sources are transcoded to H.264 once and cached by content
        hash so a 250-case realtime batch does not re-transcode the same feed.
        MJPEG/other still-image blobs (some feeds are static poster images that
        are intentionally bound as the video input for image-to-video) are
        looped into a short H.264 clip so the browser SDK can open a track.
        Returns ``(path, transcoded)``.
        """

        codec = self._video_codec(input_path)
        if codec in {"h264", "avc1", "h264_", None}:
            # Unknown codec probe (no ffprobe) still goes through as-is; the
            # harness reports a clear error if the browser rejects it.
            return input_path, False
        cache_root = (self._root / _H264_TRANSCODE_DIR).resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        cached = cache_root / f"{sha256}.mp4"
        if cached.is_file() and cached.stat().st_size:
            return cached, True
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise MissingDependencyError(
                "ffmpeg is required to transcode HEVC inputs for realtime generation"
            )
        # Still-image codecs (mjpeg, png, etc.) have no duration/fps and odd
        # dimensions are common (e.g. 1080x2337); libx264 requires even
        # height. Loop the frame into a short clip with even dimensions.
        still = codec in {"mjpeg", "png", "bmp", "gif", "webp"}
        temporary = cached.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}.mp4")
        command = [
            ffmpeg,
            "-y",
        ]
        if still:
            command += ["-loop", "1", "-framerate", "25", "-t", "5", "-i", str(input_path)]
        else:
            command += ["-i", str(input_path)]
        command += [
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExternalServiceError(f"realtime transcode failed: {exc}") from exc
        if completed.returncode != 0 or not temporary.is_file():
            raise ExternalServiceError(
                f"realtime transcode failed: {completed.stderr[-2000:]}"
            )
        os.replace(temporary, cached)
        return cached, True

    def run_case(self, case: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from shutil import which

        if which("node") is None:
            raise MissingDependencyError("node is required for realtime generation")
        input_asset = self._repository.get_asset(case["edited_video_asset_id"])
        input_path = Path(self._artifacts.resolve(input_asset["uri"])).resolve()
        input_sha256 = input_asset.get("sha256") or _file_sha256(input_path)
        input_path, _transcoded = self._ensure_browser_compatible(input_path, input_sha256)
        reference_path = None
        for asset_id in case.get("prompt_asset_ids", []):
            asset = self._repository.get_asset(asset_id)
            if asset.get("kind") == "prompt_image":
                reference_path = Path(self._artifacts.resolve(asset["uri"])).resolve()
                break
        with tempfile.TemporaryDirectory(prefix="xmax-realtime-") as directory:
            temp = Path(directory)
            request_path = temp / "request.json"
            output_json = temp / "result.json"
            output_video = temp / "result.webm"
            request = {
                "input_path": str(input_path),
                "input_method": case.get("api_asset_bindings", {}).get(
                    "input_method", "connectMedia"
                ),
                "reference_path": str(reference_path) if reference_path else None,
                "reference_url": config.get("reference_url"),
                "prompt": case.get("prompt_text", ""),
                "model": case.get("model_id", "x2.0"),
                "duration_s": config.get(
                    "duration_s", input_asset.get("media", {}).get("duration_s", 3)
                ),
                "tracks": config.get("tracks", []),
                "base_url": config.get("base_url"),
                "headed": config.get("headed", self._headed),
                "output_json_path": str(output_json),
                "output_video_path": str(output_video),
            }
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            command = ["npm", "run", "harness", "--", "--config", str(request_path)]
            environment = os.environ.copy()
            if self._api_key:
                environment["XMAX_API_KEY"] = self._api_key
            try:
                completed = subprocess.run(
                    command,
                    cwd=self._root / "realtime-harness",
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    env=environment,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ExternalServiceError(f"realtime harness failed: {exc}") from exc
            if completed.returncode != 0 or not output_json.is_file():
                raise ExternalServiceError(
                    f"realtime harness exited {completed.returncode}: {completed.stderr[-2000:]}"
                )
            result = json.loads(output_json.read_text(encoding="utf-8"))
            if output_video.is_file() and output_video.stat().st_size:
                stored = self._artifacts.put_file(
                    "runs",
                    output_video,
                    f"realtime/{case['case_id']}/{uuid.uuid4().hex}/result.webm",
                )
                result["recording_uri"] = stored["uri"]
            return result
