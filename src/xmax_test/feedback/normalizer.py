"""Human feedback normalizer.

Maps raw human text to existing dimension labels, partial matches, unmapped
proposals or clarification requests. Raw text is never modified; normalized
labels are derived and versioned. Low-confidence records never enter the
learning pool.
"""

from __future__ import annotations

import json
from typing import Any

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

    def normalize(self, raw_signal: dict[str, Any], benchmark: dict[str, Any]) -> dict[str, Any]:
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
                        "not_applicable_when": criterion.get("not_applicable_when"),
                        "anchors": criterion.get("anchors", {}),
                    }
                    for criterion in item.get("criteria", [])
                ],
            }
            for item in benchmark.get("dimensions", [])
        ]
        image_paths = [str(path) for path in raw_signal.get("evidence_images", []) if path]
        prompt = (
            "你负责把一条人工视频评语转写为绝对的单视频监督标签。视频证据和人工原文是唯一主要输入。"
            "不得猜测模型版本、成功率、Feed或Prompt，也不得把‘相对减少’夸大成‘完全没有’。"
            "当原文用新版/旧版做比较时，source_attachment_label只用来确定当前视频在语句中的方向："
            "把比较结论拆成当前视频的独立positive/negative标签，不启动对比评测。"
            "如果只能知道方向而不能确定绝对程度，仍可映射现有维度并把severity设为unknown；"
            "只有连概念对应哪个维度都不能确定时才使用needs_clarification。"
            "结合可见视频证据映射到现有维度；证据不足时使用needs_clarification。"
            "每个标签给出dimension_id、criterion_id、score_hint、confidence、rationale、polarity和severity。"
            "dimension_id只能填维度ID（例如E4），criterion_id才填细则ID（例如E4.2），不得把细则ID填入dimension_id。"
            "criterion_id必须是该维度已有细则；评语和视频证据足以支持时，score_hint按现有细则的0/1/2语义给出，"
            "否则criterion_id或score_hint返回null。score_hint是待人工审核的校准提示，不是新的人工真值。"
            "polarity为positive/negative/mixed/neutral，severity为none/mild/moderate/severe/unknown。"
            "无法映射的新概念才生成dimension_proposal。只返回JSON。\n输入："
            + json.dumps(
                {
                    "raw_text": raw_signal.get("raw_text", ""),
                    "review_context": raw_signal.get("review_context", "unknown"),
                    "source_attachment_label": raw_signal.get("source_field"),
                    "pairwise_projection": raw_signal.get("comparison"),
                    "scenario": raw_signal.get("scenario"),
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
        allowed = {item["dimension_id"] for item in dimensions if item.get("dimension_id")}
        allowed_criteria = {
            item["dimension_id"]: {
                criterion["criterion_id"]
                for criterion in item.get("criteria", [])
                if criterion.get("criterion_id")
            }
            for item in dimensions
            if item.get("dimension_id")
        }
        criterion_to_dimension = {
            criterion_id: dimension_id
            for dimension_id, criterion_ids in allowed_criteria.items()
            for criterion_id in criterion_ids
        }
        labels = []
        allowed_polarities = {"positive", "negative", "mixed", "neutral"}
        allowed_severities = {"none", "mild", "moderate", "severe", "unknown"}
        for label in parsed.get("normalized_labels", []):
            dimension_id = label.get("dimension_id")
            criterion_id = label.get("criterion_id")
            if dimension_id not in allowed and dimension_id in criterion_to_dimension:
                criterion_id = criterion_id or dimension_id
                dimension_id = criterion_to_dimension[dimension_id]
            if dimension_id not in allowed:
                continue
            confidence = min(1.0, max(0.0, float(label.get("confidence", 0))))
            if criterion_id not in allowed_criteria.get(dimension_id, set()):
                criterion_id = None
            raw_score_hint = label.get("score_hint") if criterion_id else None
            score_hint = (
                raw_score_hint
                if isinstance(raw_score_hint, int)
                and not isinstance(raw_score_hint, bool)
                and raw_score_hint in {0, 1, 2}
                else None
            )
            polarity = label.get("polarity")
            if polarity not in allowed_polarities:
                polarity = "neutral"
            severity = label.get("severity")
            if severity not in allowed_severities:
                severity = "unknown"
            labels.append(
                {
                    **label,
                    "dimension_id": dimension_id,
                    "criterion_id": criterion_id,
                    "score_hint": score_hint,
                    "polarity": polarity,
                    "severity": severity,
                    "confidence": confidence,
                }
            )
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
            # MLLM mapping is a proposal, not human gold.  A separate explicit
            # review decision grants learning_permission.
            "learning_permission": False,
            "normalization_review": {
                "status": (
                    "pending"
                    if status in {"existing", "partial"}
                    and confidence >= self._threshold
                    else "not_eligible"
                ),
                "reviewer": None,
                "reviewed_at": None,
                "note": None,
            },
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


def canonicalize_normalized_signal(
    signal: dict[str, Any], benchmark: dict[str, Any]
) -> dict[str, Any]:
    """Repair provider enum/ID drift locally, without another provider call."""

    allowed_criteria = {
        item["dimension_id"]: {
            criterion["criterion_id"]
            for criterion in item.get("criteria", [])
            if criterion.get("criterion_id")
        }
        for item in benchmark.get("dimensions", [])
        if item.get("dimension_id")
    }
    criterion_to_dimension = {
        criterion_id: dimension_id
        for dimension_id, criterion_ids in allowed_criteria.items()
        for criterion_id in criterion_ids
    }
    labels = []
    for raw_label in signal.get("normalized_labels", []):
        label = dict(raw_label)
        dimension_id = label.get("dimension_id")
        criterion_id = label.get("criterion_id")
        if dimension_id not in allowed_criteria and dimension_id in criterion_to_dimension:
            criterion_id = criterion_id or dimension_id
            dimension_id = criterion_to_dimension[dimension_id]
        if dimension_id not in allowed_criteria:
            continue
        if criterion_id not in allowed_criteria[dimension_id]:
            criterion_id = None
        score_hint = label.get("score_hint") if criterion_id else None
        if (
            not isinstance(score_hint, int)
            or isinstance(score_hint, bool)
            or score_hint not in {0, 1, 2}
        ):
            score_hint = None
        polarity = label.get("polarity")
        if polarity not in {"positive", "negative", "mixed", "neutral"}:
            polarity = "neutral"
        severity = label.get("severity")
        if severity not in {"none", "mild", "moderate", "severe", "unknown"}:
            severity = "unknown"
        confidence = min(1.0, max(0.0, float(label.get("confidence", 0))))
        labels.append(
            {
                **label,
                "dimension_id": dimension_id,
                "criterion_id": criterion_id,
                "score_hint": score_hint,
                "polarity": polarity,
                "severity": severity,
                "confidence": confidence,
            }
        )
    status = signal.get("mapping_status")
    if status not in {"existing", "partial", "unmapped", "needs_clarification"}:
        status = "needs_clarification"
    updated = {**signal, "mapping_status": status, "normalized_labels": labels}
    updated["normalized_hash"] = content_hash(
        {
            "signal_id": updated["signal_id"],
            "labels": labels,
            "status": status,
            "normalizer_id": updated.get("normalizer_id"),
            "normalizer_version": updated.get("normalizer_version"),
        }
    )
    return updated


class RuleNormalizer:
    """Deterministic fallback normalizer used by tests and dry-runs.

    Maps raw text to existing dimensions by keyword; everything else becomes
    ``unmapped`` with a proposal hook.
    """

    def __init__(self, benchmark: dict[str, Any], threshold: float = 0.7) -> None:
        self._benchmark = benchmark
        self._threshold = threshold

    def normalize(
        self, raw_signal: dict[str, Any], benchmark: dict[str, Any] | None = None
    ) -> dict[str, Any]:
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
    words = list(dimension.get("keywords", []))
    for token in definition.split("；"):
        words.append(token[:6])
    words.insert(0, name[:6] if name else "")
    words.extend(
        criterion.get("name", "")
        for criterion in dimension.get("criteria", [])
        if criterion.get("name")
    )
    return [word for word in words if word]


def _human_mlmm_output_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["mapping_status", "normalized_labels", "dimension_proposal"],
        "properties": {
            "mapping_status": {"enum": ["existing", "partial", "unmapped", "needs_clarification"]},
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
                        "polarity": {"enum": ["positive", "negative", "mixed", "neutral"]},
                        "severity": {"enum": ["none", "mild", "moderate", "severe", "unknown"]},
                        "criterion_id": {"type": ["string", "null"]},
                        "score_hint": {
                            "type": ["integer", "null"],
                            "minimum": 0,
                            "maximum": 2,
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
