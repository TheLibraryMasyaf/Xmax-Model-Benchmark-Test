"""Subprocess bridge to the browser-based realtime harness."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ...errors import ExternalServiceError, MissingDependencyError


class BrowserRealtimeHarness:
    def __init__(
        self, project_root: Path, artifacts: Any, repository: Any,
        *, api_key: str | None = None, timeout_s: int = 900, headed: bool = False,
    ) -> None:
        self._root = Path(project_root)
        self._artifacts = artifacts
        self._repository = repository
        self._timeout = timeout_s
        self._headed = headed
        self._api_key = api_key

    def run_case(self, case: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from shutil import which

        if which("node") is None:
            raise MissingDependencyError("node is required for realtime generation")
        input_asset = self._repository.get_asset(case["edited_video_asset_id"])
        input_path = self._artifacts.resolve(input_asset["uri"])
        reference_path = None
        for asset_id in case.get("prompt_asset_ids", []):
            asset = self._repository.get_asset(asset_id)
            if asset.get("kind") == "prompt_image":
                reference_path = self._artifacts.resolve(asset["uri"])
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
                "duration_s": config.get("duration_s", input_asset.get("media", {}).get("duration_s", 3)),
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
                    command, cwd=self._root / "realtime-harness", capture_output=True,
                    text=True, timeout=self._timeout, env=environment,
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
