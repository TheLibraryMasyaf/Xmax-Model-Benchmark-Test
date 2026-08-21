"""Local Codex CLI implementation of the provider-neutral MLLM protocol."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from shutil import which
from typing import Any

from ...errors import ExternalServiceError, MissingDependencyError
from ...time import utc_now
from .base import MlmmResponse


class CodexCliProvider:
    def __init__(
        self,
        *,
        binary: str = "codex",
        timeout_s: int = 300,
        sandbox: str = "read-only",
        model: str | None = None,
    ) -> None:
        self._binary = binary
        self._timeout = timeout_s
        self._sandbox = sandbox
        self._model = model

    @property
    def provider_id(self) -> str:
        return "codex_cli"

    def complete_json(
        self,
        *,
        prompt: str,
        image_paths: list[str],
        output_schema: dict[str, Any],
        media_inputs: list[dict[str, Any]] | None = None,
    ) -> MlmmResponse:
        if which(self._binary) is None:
            raise MissingDependencyError(f"codex binary not found: {self._binary}")
        with tempfile.TemporaryDirectory(prefix="xmax-mlmm-codex-") as directory:
            schema_path = Path(directory) / "output.schema.json"
            schema_path.write_text(json.dumps(output_schema, ensure_ascii=False), encoding="utf-8")
            command = [
                self._binary,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-rules",
                "--color",
                "never",
                "--sandbox",
                self._sandbox,
                "--output-schema",
                str(schema_path),
            ]
            if self._model:
                command += ["--model", self._model]
            for image in image_paths:
                command += ["-i", str(Path(image).resolve())]
            started = time.monotonic()
            try:
                result = subprocess.run(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    cwd=directory,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ExternalServiceError(f"codex exec failed: {exc}") from exc
        if result.returncode != 0:
            raise ExternalServiceError(f"codex exited {result.returncode}: {result.stderr[-500:]}")
        raw_text = result.stdout.strip()
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ExternalServiceError(f"codex returned non-JSON: {exc}") from exc
        return MlmmResponse(
            payload=payload,
            raw_text=raw_text,
            provider_id=self.provider_id,
            model=self._model,
            metadata={
                "duration_s": round(time.monotonic() - started, 3),
                "started_at": utc_now(),
                "stderr": result.stderr,
                "exit_code": result.returncode,
            },
        )
