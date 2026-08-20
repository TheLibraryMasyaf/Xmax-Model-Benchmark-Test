"""Codex CLI MLLM judge adapter.

Runs the local ``codex exec`` binary, parses strict JSON, validates it against
the judgment schema, and retries on non-JSON output. Raw stdout/stderr, exit
code, duration and prompt hash are always preserved.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..errors import ExternalServiceError, MissingDependencyError, ValidationError
from ..hashing import content_hash, sha256_text
from ..time import utc_now

JUDGMENT_REQUIRED = {
    "evaluation_id",
    "run_id",
    "benchmark_version",
    "dimension_id",
    "dimension_version",
    "judge_id",
    "judge_version",
    "verdict",
    "evidence",
}


class CodexCliJudge:
    def __init__(
        self,
        *,
        binary: str = "codex",
        timeout_s: int = 300,
        max_retries: int = 2,
        work_dir: str | Path | None = None,
        artifact_store: Any = None,
        sandbox: str = "read-only",
        log_dir: str | Path | None = None,
        judge_id: str = "codex-mlmm",
        version: str = "draft",
        supported_dimensions: list[str] | None = None,
        supported_modes: list[str] | None = None,
    ) -> None:
        self._binary = binary
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._work_dir = str(work_dir) if work_dir else None
        self._artifacts = artifact_store
        self._sandbox = sandbox
        self._log_dir = Path(log_dir) if log_dir else None
        self._judge_id = judge_id
        self._version = version
        self._supported_dimensions = list(supported_dimensions or [])
        self._supported_modes = list(supported_modes or ["offline", "realtime"])

    def manifest(self) -> dict[str, Any]:
        return {
            "judge_id": self._judge_id,
            "version": self._version,
            "kind": "mlmm_cli",
            "supported_dimensions": self._supported_dimensions,
            "supported_modes": self._supported_modes,
            "required_inputs": ["feed_evidence", "prompt", "result_evidence"],
            "entrypoint": self._binary,
            "timeout_seconds": self._timeout,
        }

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Run one Codex evaluation and return normalized judgments."""

        prompt = context.get("prompt", "")
        prompt_hash = sha256_text(prompt)
        evidence_images = context.get("evidence_images", [])
        output_schema = context.get("output_schema", {})
        attempts = 0
        while True:
            attempts += 1
            raw = self._run_codex(prompt, evidence_images, output_schema)
            parsed, error = self._parse(raw, output_schema)
            self._save_raw(attempts, prompt_hash, raw, error)
            if parsed is not None:
                break
            if attempts > self._max_retries:
                raise ExternalServiceError(
                    f"codex judge returned no valid JSON after {attempts} attempts: {error}"
                )
        return self._normalize(parsed, context, prompt_hash, raw)

    def _run_codex(
        self,
        prompt: str,
        evidence_images: list[str],
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        from shutil import which

        if which(self._binary) is None:
            raise MissingDependencyError(f"codex binary not found: {self._binary}")
        with tempfile.TemporaryDirectory(prefix="xmax-codex-judge-") as directory:
            schema_path = Path(directory) / "judgment-output.schema.json"
            schema_path.write_text(
                json.dumps(
                    _model_output_schema(output_schema), ensure_ascii=False
                ),
                encoding="utf-8",
            )
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
            for image in evidence_images:
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
        duration_s = round(time.monotonic() - started, 3)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "duration_s": duration_s,
            "started_at": utc_now(),
        }

    def _parse(self, raw: dict[str, Any], output_schema: dict[str, Any]) -> tuple[Any, str | None]:
        if raw.get("exit_code") != 0:
            return None, f"codex exited {raw['exit_code']}: {raw.get('stderr', '')[:500]}"
        text = raw.get("stdout", "").strip()
        if not text:
            return None, "empty stdout"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"non-JSON output: {exc}"
        return parsed, None

    def _normalize(
        self, parsed: Any, context: dict[str, Any], prompt_hash: str, raw: dict[str, Any]
    ) -> list[dict[str, Any]]:
        items = parsed if isinstance(parsed, list) else [parsed]
        judgments = []
        for item in items:
            normalized = {
                **item,
                "evaluation_id": context.get("evaluation_id", item.get("evaluation_id")),
                "run_id": context.get("run_id", item.get("run_id")),
                "benchmark_version": context.get("benchmark_version", item.get("benchmark_version")),
                "dimension_id": context.get("dimension_id", item.get("dimension_id")),
                "dimension_version": context.get("dimension_version", item.get("dimension_version")),
                "judge_id": self._judge_id,
                "judge_version": self._version,
            }
            missing = {key for key in JUDGMENT_REQUIRED if normalized.get(key) is None}
            if missing:
                raise ValidationError(
                    f"codex judgment missing fields: {sorted(missing)}"
                )
            judgment = {
                    **normalized,
                    "prompt_hash": prompt_hash,
                    "raw_output_uri": self._raw_uri(raw),
                }
            output_schema = context.get("output_schema") or {}
            if output_schema:
                from jsonschema import Draft202012Validator

                errors = sorted(
                    Draft202012Validator(output_schema).iter_errors(judgment),
                    key=lambda error: list(error.path),
                )
                if errors:
                    raise ValidationError(f"codex judgment schema error: {errors[0].message}")
            judgments.append(judgment)
        return judgments

    def _raw_uri(self, raw: dict[str, Any]) -> str | None:
        if self._artifacts is None:
            return None
        stored = self._artifacts.put_bytes(
            "evaluations",
            f"raw/codex-{content_hash(raw)}.json",
            json.dumps(raw, ensure_ascii=False).encode("utf-8"),
        )
        return stored["uri"]

    def _save_raw(self, attempt: int, prompt_hash: str, raw: dict[str, Any], error: str | None) -> None:
        if self._log_dir is None:
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "attempt": attempt,
            "prompt_hash": prompt_hash,
            "exit_code": raw.get("exit_code"),
            "duration_s": raw.get("duration_s"),
            "error": error,
            "stdout_tail": raw.get("stdout", "")[-2000:],
        }
        with (self._log_dir / "codex-events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


class FakeCodexCliJudge:
    """Deterministic codex fake: valid JSON, malformed, retryable, permanent."""

    def __init__(
        self,
        *,
        outputs: list[Any] | None = None,
        always_invalid: bool = False,
        exit_code: int = 0,
    ) -> None:
        self._outputs = outputs or [
            [
                {
                    "evaluation_id": "eval-1",
                    "run_id": "run-1",
                    "benchmark_version": "0.1.0-draft",
                    "dimension_id": "C2",
                    "dimension_version": "0.1.0-draft",
                    "judge_id": "codex-mlmm",
                    "judge_version": "draft",
                    "verdict": "ok",
                    "score": 1.5,
                    "confidence": 0.8,
                    "evidence": [{"description": "complies"}],
                }
            ]
        ]
        self._always_invalid = always_invalid
        self._exit_code = exit_code
        self.attempts = 0
        self.prompt_hashes: list[str] = []

    def manifest(self) -> dict[str, Any]:
        return {
            "judge_id": "codex-mlmm",
            "version": "draft",
            "kind": "mlmm_cli",
            "supported_dimensions": [],
            "supported_modes": ["offline", "realtime"],
            "required_inputs": ["feed_evidence", "prompt", "result_evidence"],
            "entrypoint": "fake-codex",
            "timeout_seconds": 10,
        }

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        self.attempts += 1
        self.prompt_hashes.append(sha256_text(context.get("prompt", "")))
        if self._always_invalid:
            raise ExternalServiceError("codex returned invalid JSON permanently")
        if self._exit_code != 0:
            raise ExternalServiceError(f"codex exited {self._exit_code}")
        output = self._outputs[min(self.attempts - 1, len(self._outputs) - 1)]
        items = output if isinstance(output, list) else [output]
        return [
            {
                **item,
                "evaluation_id": context.get("evaluation_id", item.get("evaluation_id")),
                "run_id": context.get("run_id", item.get("run_id")),
                "benchmark_version": context.get("benchmark_version", item.get("benchmark_version")),
                "judge_id": "codex-mlmm",
                "judge_version": "draft",
            }
            for item in items
        ]


def _model_output_schema(full_schema: dict[str, Any]) -> dict[str, Any]:
    """Constrain only fields the model owns; identity is injected locally."""

    properties = full_schema.get("properties", {}) if isinstance(full_schema, dict) else {}
    model_fields = (
        "verdict",
        "score",
        "confidence",
        "assessable",
        "evidence",
        "raw_metrics",
    )
    selected = {key: properties[key] for key in model_fields if key in properties}
    if not selected:
        return {"type": "object"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            key for key in ("verdict", "assessable", "evidence") if key in selected
        ],
        "properties": selected,
        "additionalProperties": False,
    }
