"""Human feedback normalizer.

Maps raw human text to existing dimension labels, partial matches, unmapped
proposals or clarification requests. Raw text is never modified; normalized
labels are derived and versioned. Low-confidence records never enter the
learning pool.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from shutil import which

from ..errors import ContractError, ExternalServiceError, MissingDependencyError
from ..hashing import content_hash


class MlmmHumanNormalizer:
    """Provider-neutral normalizer for the minimal video + comment contract."""

    def __init__(
        self,
        provider: Any,
        *,
        threshold: float = 0.7,
        version: str = "2.0.0",
    ) -> None:
        self._provider = provider
        self._threshold = threshold
        self._version = version

    def normalize(
        self, raw_signal: dict[str, Any], benchmark: dict[str, Any]
    ) -> dict[str, Any]:
        dimensions = [
            {
                "dimension_id": item.get("dimension_id"),
                "name": item.get("name"),
                "definition": item.get("definition"),
                "criteria": [
                    {
                        "criterion_id": criterion.get("criterion_id"),
                        "name": criterion.get("name"),
                        "definition": criterion.get("definition"),
                    }
                    for criterion in item.get("criteria", [])
                ],
            }
            for item in benchmark.get("dimensions", [])
        ]
        image_paths = [
            str(path) for path in raw_signal.get("evidence_images", []) if path
        ]
        prompt = (
            "你负责把一条人工视频评语转写为绝对的单视频监督标签。视频证据和人工原文是唯一主要输入。"
            "不得猜测模型版本、成功率、Feed或Prompt，也不得把‘相对减少’夸大成‘完全没有’。"
            "当原文用新版/旧版做比较时，source_attachment_label只用来确定当前视频在语句中的方向："
            "把比较结论拆成当前视频的独立positive/negative标签，不启动对比评测。"
            "如果只能知道方向而不能确定绝对程度，仍可映射现有维度并把severity设为unknown；"
            "只有连概念对应哪个维度都不能确定时才使用needs_clarification。"
            "结合可见视频证据映射到现有维度；证据不足时使用needs_clarification。"
            "每个标签给出dimension_id、confidence、rationale、polarity和severity。"
            "polarity为positive/negative/mixed/neutral，severity为none/mild/moderate/severe/unknown。"
            "无法映射的新概念才生成dimension_proposal。只返回JSON。\n输入："
            + json.dumps(
                {
                    "raw_text": raw_signal.get("raw_text", ""),
                    "review_context": raw_signal.get("review_context", "unknown"),
                    "source_attachment_label": raw_signal.get("source_field"),
                    "video_evidence_image_count": len(image_paths),
                    "dimensions": dimensions,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        response = self._provider.complete_json(
            prompt=prompt,
            image_paths=image_paths,
            output_schema=_human_mlmm_output_schema(),
        )
        parsed = response.payload
        allowed = {
            item["dimension_id"] for item in dimensions if item.get("dimension_id")
        }
        labels = []
        for label in parsed.get("normalized_labels", []):
            if label.get("dimension_id") not in allowed:
                continue
            confidence = min(1.0, max(0.0, float(label.get("confidence", 0))))
            labels.append({**label, "confidence": confidence})
        status = parsed.get("mapping_status", "needs_clarification")
        confidence = max((item["confidence"] for item in labels), default=0.0)
        normalized = {
            **raw_signal,
            "signal_id": raw_signal["signal_id"],
            "raw_text": raw_signal.get("raw_text", ""),
            "mapping_status": status,
            "normalized_labels": labels,
            "dimension_proposal": parsed.get("dimension_proposal"),
            "learning_permission": status in {"existing", "partial"}
            and confidence >= self._threshold,
            "normalizer_id": f"mlmm-provider:{response.provider_id}",
            "normalizer_version": self._version,
            "normalizer_model": response.model,
            "normalizer_usage": response.usage,
            "normalizer_raw_response": response.raw_text,
        }
        normalized["normalized_hash"] = content_hash(
            {
                "signal_id": normalized["signal_id"],
                "labels": labels,
                "status": status,
                "normalizer_id": normalized["normalizer_id"],
                "normalizer_version": self._version,
            }
        )
        return normalized


class CodexNormalizer:
    """Normalize immutable human text with local Codex CLI.

    Codex may only derive labels/proposals. Identity fields and the original
    text are restored from ``raw_signal`` so model output cannot rewrite them.
    """

    def __init__(
        self,
        *,
        binary: str = "codex",
        timeout_s: int = 180,
        threshold: float = 0.7,
        version: str = "1.0.0",
        work_dir: str | None = None,
    ) -> None:
        self._binary = binary
        self._timeout_s = timeout_s
        self._threshold = threshold
        self._version = version
        self._work_dir = work_dir

    def normalize(self, raw_signal: dict[str, Any], benchmark: dict[str, Any]) -> dict[str, Any]:
        if which(self._binary) is None:
            raise MissingDependencyError(f"codex binary not found: {self._binary}")
        dimensions = [
            {
                "dimension_id": item.get("dimension_id"),
                "name": item.get("name"),
                "definition": item.get("definition"),
            }
            for item in benchmark.get("dimensions", [])
        ]
        prompt = (
            "You normalize one human video-evaluation comment. Return JSON only. "
            "Do not invent a score. mapping_status must be existing, partial, "
            "unmapped, or needs_clarification. normalized_labels is an array of "
            "{dimension_id, confidence, rationale}; confidence is 0..1. Use only "
            "listed dimension_ids. If the text expresses a missing concept, set "
            "dimension_proposal to {name, definition, status:'proposed'}; otherwise null.\n"
            + json.dumps(
                {
                    "raw_text": raw_signal.get("raw_text", ""),
                    "review_context": raw_signal.get("review_context", "unknown"),
                    "dimensions": dimensions,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        with tempfile.TemporaryDirectory(prefix="xmax-codex-normalizer-") as directory:
            schema_path = Path(directory) / "normalizer-output.schema.json"
            schema_path.write_text(
                json.dumps(_normalizer_output_schema(), ensure_ascii=False),
                encoding="utf-8",
            )
            try:
                result = subprocess.run(
                    [
                        self._binary,
                        "exec",
                        "--skip-git-repo-check",
                        "--ephemeral",
                        "--ignore-rules",
                        "--color",
                        "never",
                        "--sandbox",
                        "read-only",
                        "--output-schema",
                        str(schema_path),
                        prompt,
                    ],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_s,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ExternalServiceError(f"codex normalizer failed: {exc}") from exc
        if result.returncode != 0:
            raise ExternalServiceError(
                f"codex normalizer exited {result.returncode}: {result.stderr[-500:]}"
            )
        try:
            parsed = json.loads(result.stdout.strip())
        except json.JSONDecodeError as exc:
            raise ExternalServiceError(f"codex normalizer returned non-JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ContractError("codex normalizer output must be a JSON object")
        allowed = {item["dimension_id"] for item in dimensions if item.get("dimension_id")}
        labels = []
        for label in parsed.get("normalized_labels", []):
            if label.get("dimension_id") not in allowed:
                continue
            confidence = float(label.get("confidence", 0.0))
            labels.append({**label, "confidence": min(1.0, max(0.0, confidence))})
        status = parsed.get("mapping_status", "needs_clarification")
        if status not in {"existing", "partial", "unmapped", "needs_clarification"}:
            status = "needs_clarification"
        confidence = max((item["confidence"] for item in labels), default=0.0)
        normalized = {
            **raw_signal,
            "signal_id": raw_signal["signal_id"],
            "raw_text": raw_signal.get("raw_text", ""),
            "mapping_status": status,
            "normalized_labels": labels,
            "dimension_proposal": parsed.get("dimension_proposal"),
            "learning_permission": status in {"existing", "partial"} and confidence >= self._threshold,
            "normalizer_id": "codex-cli-normalizer",
            "normalizer_version": self._version,
        }
        normalized["normalized_hash"] = content_hash(
            {
                "signal_id": normalized["signal_id"],
                "labels": labels,
                "status": status,
                "normalizer_version": self._version,
            }
        )
        return normalized


class RuleNormalizer:
    """Deterministic fallback normalizer used by tests and dry-runs.

    Maps raw text to existing dimensions by keyword; everything else becomes
    ``unmapped`` with a proposal hook.
    """

    def __init__(self, benchmark: dict[str, Any], threshold: float = 0.7) -> None:
        self._benchmark = benchmark
        self._threshold = threshold

    def normalize(self, raw_signal: dict[str, Any], benchmark: dict[str, Any] | None = None) -> dict[str, Any]:
        benchmark = benchmark or self._benchmark
        raw_text = raw_signal.get("raw_text", "")
        labels: list[dict[str, Any]] = []
        for dimension in benchmark.get("dimensions", []):
            for keyword in _keywords(dimension):
                if keyword and keyword in raw_text:
                    labels.append(
                        {
                            "dimension_id": dimension["dimension_id"],
                            "confidence": 0.8,
                            "matched_keyword": keyword,
                        }
                    )
                    break
        if labels:
            status = "existing"
            confidence = max(item["confidence"] for item in labels)
        else:
            status = "unmapped"
            confidence = 0.0
        return {
            "signal_id": raw_signal["signal_id"],
            "sample_id": raw_signal.get("sample_id", ""),
            "raw_text": raw_text,
            "review_context": raw_signal.get("review_context", "unknown"),
            "mapping_status": status,
            "normalized_labels": labels,
            "dimension_proposal": (
                {"name": raw_text[:80], "definition": raw_text, "status": "proposed"}
                if status == "unmapped"
                else None
            ),
            "learning_permission": confidence >= self._threshold,
            "normalizer_id": "rule-normalizer",
            "normalizer_version": "0.1.0",
            "normalized_hash": content_hash(
                {"signal": raw_signal["signal_id"], "labels": labels, "status": status}
            ),
        }


def _keywords(dimension: dict[str, Any]) -> list[str]:
    definition = dimension.get("definition", "")
    name = dimension.get("name", "")
    words = []
    for token in definition.split("；"):
        words.append(token[:6])
    words.insert(0, name[:6] if name else "")
    return [word for word in words if word]


def _normalizer_output_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["mapping_status", "normalized_labels", "dimension_proposal"],
        "properties": {
            "mapping_status": {
                "enum": ["existing", "partial", "unmapped", "needs_clarification"]
            },
            "normalized_labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["dimension_id", "confidence", "rationale"],
                    "properties": {
                        "dimension_id": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "dimension_proposal": _dimension_proposal_schema(),
        },
        "additionalProperties": False,
    }


def _human_mlmm_output_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["mapping_status", "normalized_labels", "dimension_proposal"],
        "properties": {
            "mapping_status": {
                "enum": ["existing", "partial", "unmapped", "needs_clarification"]
            },
            "normalized_labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "dimension_id",
                        "confidence",
                        "rationale",
                        "polarity",
                        "severity",
                    ],
                    "properties": {
                        "dimension_id": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string"},
                        "polarity": {
                            "enum": ["positive", "negative", "mixed", "neutral"]
                        },
                        "severity": {
                            "enum": ["none", "mild", "moderate", "severe", "unknown"]
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "dimension_proposal": _dimension_proposal_schema(),
        },
        "additionalProperties": False,
    }


def _dimension_proposal_schema() -> dict[str, Any]:
    return {
        "anyOf": [
            {
                "type": "object",
                "required": ["name", "definition", "status"],
                "properties": {
                    "name": {"type": "string"},
                    "definition": {"type": "string"},
                    "status": {"type": "string", "const": "proposed"},
                },
                "additionalProperties": False,
            },
            {"type": "null"},
        ]
    }
