"""Provider-neutral MLLM Judge with one-call multi-dimension output."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ...errors import (
    EvaluationBudgetPausedError,
    EvaluationInfrastructurePausedError,
    ExternalServiceError,
    MlmmInvalidRequestError,
    MlmmTimeoutError,
    ValidationError,
)
from ...hashing import content_hash, sha256_text
from ...time import utc_now


class MlmmJudge:
    def __init__(
        self,
        provider: Any,
        *,
        judge_id: str,
        version: str,
        supported_dimensions: list[str],
        supported_criteria: list[str] | None = None,
        supported_modes: list[str],
        max_retries: int = 2,
        artifact_store: Any = None,
        log_dir: str | Path | None = None,
        calibration_path: str | Path | None = None,
        max_examples_per_dimension: int = 2,
    ) -> None:
        self._provider = provider
        self._judge_id = judge_id
        self._version = version
        self._supported_dimensions = list(supported_dimensions)
        self._supported_criteria = list(supported_criteria or [])
        self._supported_modes = list(supported_modes)
        self._max_retries = max_retries
        self._artifacts = artifact_store
        self._log_dir = Path(log_dir) if log_dir else None
        self._calibration_path = Path(calibration_path) if calibration_path else None
        self._max_examples_per_dimension = max(0, int(max_examples_per_dimension))

    def manifest(self) -> dict[str, Any]:
        return {
            "judge_id": self._judge_id,
            "version": self._version,
            "kind": "mlmm",
            "batch_dimensions": True,
            "provider_id": self._provider.provider_id,
            "supported_dimensions": self._supported_dimensions,
            "supported_criteria": self._supported_criteria,
            "supported_modes": self._supported_modes,
            "required_inputs": ["feed_evidence", "prompt", "result_evidence"],
        }

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        contracts = list(context.get("dimension_contracts") or [])
        if not contracts and context.get("dimension_id"):
            contracts = [
                {
                    "dimension_id": context["dimension_id"],
                    "version": context.get("dimension_version", ""),
                }
            ]
        allowed = {item["dimension_id"]: item.get("version", "") for item in contracts}
        criteria_by_dimension = {
            item["dimension_id"]: [
                criterion["criterion_id"]
                for criterion in item.get("criteria", [])
                if criterion.get("criterion_id")
            ]
            for item in contracts
        }
        if not allowed:
            raise ValidationError("MLLM Judge requires at least one dimension contract")
        prompt = context.get("prompt", "")
        prompt = self._append_human_calibration(prompt, sorted(allowed))
        prompt_hash = sha256_text(prompt)
        schema = _batch_output_schema(criteria_by_dimension)
        response = None
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            started = time.monotonic()
            self._log_event(
                "request_started",
                prompt_hash=prompt_hash,
                run_id=context.get("run_id", ""),
                attempt=attempt,
            )
            try:
                response = self._provider.complete_json(
                    prompt=prompt,
                    image_paths=list(context.get("evidence_images", [])),
                    output_schema=schema,
                    media_inputs=list(context.get("media_inputs", [])),
                )
                errors = sorted(
                    Draft202012Validator(schema).iter_errors(response.payload),
                    key=lambda error: list(error.path),
                )
                if errors:
                    if response.metadata.get("paid_fallback") is True:
                        raise EvaluationInfrastructurePausedError(
                            "paid MLLM output failed local schema validation; "
                            "automatic paid retry is disabled"
                        )
                    raise ValidationError(f"MLLM output schema error: {errors[0].message}")
                self._log_event(
                    "request_succeeded",
                    prompt_hash=prompt_hash,
                    run_id=context.get("run_id", ""),
                    attempt=attempt,
                    duration_seconds=round(time.monotonic() - started, 3),
                    provider_id=response.provider_id,
                    model=response.model,
                    usage=response.usage,
                )
                break
            except Exception as exc:
                self._log_event(
                    "request_failed",
                    prompt_hash=prompt_hash,
                    run_id=context.get("run_id", ""),
                    attempt=attempt,
                    duration_seconds=round(time.monotonic() - started, 3),
                    error_code=getattr(exc, "code", type(exc).__name__),
                    error=str(exc),
                )
                last_error = exc
                response = None
                # Provider-level transport retries are already bounded.  A
                # batch pause, timeout or permanent request rejection must not
                # be multiplied again by the Judge's schema-output retry loop.
                if isinstance(
                    exc,
                    (
                        EvaluationBudgetPausedError,
                        EvaluationInfrastructurePausedError,
                        MlmmInvalidRequestError,
                        MlmmTimeoutError,
                    ),
                ):
                    raise
        if response is None:
            raise ExternalServiceError(
                f"MLLM provider produced no valid result after retries: {last_error}"
            )
        raw_uri = self._save_raw(response, prompt_hash)
        results = []
        seen: set[str] = set()
        for item in response.payload["judgments"]:
            dimension_id = item["dimension_id"]
            if dimension_id in seen:
                raise ValidationError(f"MLLM returned duplicate dimension: {dimension_id}")
            seen.add(dimension_id)
            results.append(
                {
                    **item,
                    "evaluation_id": context.get("evaluation_id", ""),
                    "run_id": context.get("run_id", ""),
                    "benchmark_version": context.get("benchmark_version", ""),
                    "dimension_version": allowed[dimension_id],
                    "judge_id": self._judge_id,
                    "judge_version": self._version,
                    "prompt_hash": prompt_hash,
                    "raw_output_uri": raw_uri,
                    "provider_id": response.provider_id,
                    "provider_model": response.model,
                    "provider_usage": response.usage,
                }
            )
        missing = set(allowed) - seen
        if missing:
            raise ValidationError(f"MLLM omitted dimensions: {sorted(missing)}")
        return results

    def _append_human_calibration(self, prompt: str, dimension_ids: list[str]) -> str:
        """Append anonymous Train-only human anchors to the blind-judge prompt.

        Calibration and Holdout packets are deliberately rejected here. Source,
        model and record identifiers are never exposed to the Judge.
        """

        if self._calibration_path is None or self._max_examples_per_dimension == 0:
            return prompt
        if not self._calibration_path.is_file():
            return prompt
        selected: dict[str, list[dict[str, Any]]] = {
            dimension_id: [] for dimension_id in dimension_ids
        }
        with self._calibration_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                packet = json.loads(line)
                dimension_id = packet.get("dimension_id")
                if dimension_id not in selected:
                    continue
                if packet.get("data_partition") != "train":
                    continue
                if packet.get("training_eligible") is not True:
                    continue
                examples = selected[dimension_id]
                if len(examples) >= self._max_examples_per_dimension:
                    continue
                supervision = packet.get("supervision") or {}
                label = supervision.get("label") or {}
                examples.append(
                    {
                        "dimension_id": dimension_id,
                        "human_comment": supervision.get("raw_text"),
                        "polarity": label.get("polarity"),
                        "severity": label.get("severity"),
                        "rationale": label.get("rationale"),
                    }
                )
        anchors = [item for items in selected.values() for item in items]
        if not anchors:
            return prompt
        return (
            f"{prompt}\n\n"
            "人工校准锚点（仅是一般评分示例，不代表当前样本；"
            "不得推断模型身份或预期输赢）：\n"
            f"{json.dumps(anchors, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _save_raw(self, response: Any, prompt_hash: str) -> str | None:
        record = {
            "provider_id": response.provider_id,
            "model": response.model,
            "usage": response.usage,
            "metadata": response.metadata,
            "raw_text": response.raw_text,
            "prompt_hash": prompt_hash,
        }
        if self._artifacts is None:
            return None
        stored = self._artifacts.put_bytes(
            "evaluations",
            f"raw/mlmm-{content_hash(record)}.json",
            json.dumps(record, ensure_ascii=False).encode("utf-8"),
        )
        return stored["uri"]

    def _log_event(self, event: str, **payload: Any) -> None:
        if self._log_dir is None:
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        record = {"event": event, "timestamp": utc_now(), **payload}
        with (self._log_dir / "mlmm-events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _batch_output_schema(criteria_by_dimension: dict[str, list[str]]) -> dict[str, Any]:
    dimension_ids = sorted(criteria_by_dimension)

    def judgment_schema(dimension_id: str) -> dict[str, Any]:
        criterion_ids = criteria_by_dimension[dimension_id]
        return {
            "type": "object",
            "required": [
                "dimension_id",
                "verdict",
                "confidence",
                "assessable",
                "evidence",
                "criterion_results",
            ],
            "properties": {
                "dimension_id": {"const": dimension_id},
                "verdict": {"type": "string"},
                "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                "assessable": {"type": "boolean"},
                "evidence": {"type": "array", "items": _evidence_schema()},
                "criterion_results": {
                    "type": "array",
                    "minItems": len(criterion_ids),
                    "maxItems": len(criterion_ids),
                    "items": {
                        "type": "object",
                        "required": [
                            "criterion_id",
                            "verdict",
                            "score",
                            "confidence",
                            "assessable",
                            "evidence",
                        ],
                        "properties": {
                            "criterion_id": {"enum": criterion_ids},
                            "verdict": {"type": "string"},
                            "score": {
                                "type": ["number", "null"],
                                "minimum": 0,
                                "maximum": 2,
                            },
                            "confidence": {
                                "type": ["number", "null"],
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "assessable": {"type": "boolean"},
                            "evidence": {"type": "array", "items": _evidence_schema()},
                            "raw_metrics": {"type": "object"},
                        },
                        "additionalProperties": False,
                    },
                },
                "raw_metrics": {"type": "object"},
            },
            "additionalProperties": False,
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["judgments"],
        "properties": {
            "judgments": {
                "type": "array",
                "minItems": len(dimension_ids),
                "maxItems": len(dimension_ids),
                "items": {"oneOf": [judgment_schema(item) for item in dimension_ids]},
            }
        },
        "additionalProperties": False,
    }


def _evidence_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["description"],
        "properties": {
            "description": {"type": "string"},
            "start_s": {"type": "number"},
            "end_s": {"type": "number"},
            "region": {"type": ["string", "object", "null"]},
        },
        "additionalProperties": False,
    }
