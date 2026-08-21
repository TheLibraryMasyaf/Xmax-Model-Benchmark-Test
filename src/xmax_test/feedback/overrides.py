"""Append-only human correction layer for AI evaluations."""

from __future__ import annotations

from typing import Any

from ..errors import ContractError
from ..hashing import content_hash
from ..time import utc_now


class HumanOverrideService:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def apply_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        evaluation_id = signal.get("evaluation_id")
        if not evaluation_id:
            raise ContractError(f"human signal {signal.get('signal_id')} has no evaluation_id")
        evaluation = self._repository.get_evaluation_result(evaluation_id)
        score = signal.get("human_score_percent")
        criterion_scores = (signal.get("annotations") or {}).get("criterion_scores", {})
        if score is None and not criterion_scores:
            raise ContractError(
                "human override requires human_score_percent or annotations.criterion_scores"
            )
        if score is not None and not 0 <= float(score) <= 100:
            raise ContractError("human_score_percent must be between 0 and 100")
        for criterion_id, value in criterion_scores.items():
            if not isinstance(value, (int, float)) or not 0 <= float(value) <= 2:
                raise ContractError(f"criterion override {criterion_id} must be on the 0..2 scale")
        known_criteria = {
            str(item.get("criterion_id"))
            for item in evaluation.get("criterion_results", [])
            if item.get("criterion_id")
        }
        unknown_criteria = sorted(set(criterion_scores) - known_criteria)
        if unknown_criteria:
            raise ContractError(
                "criterion overrides are not present in the selected Evaluation: "
                + ", ".join(unknown_criteria)
            )
        payload = {
            "override_id": "override-"
            + content_hash(
                {
                    "evaluation_id": evaluation_id,
                    "signal_id": signal["signal_id"],
                    "score": score,
                    "criterion_scores": criterion_scores,
                }
            )[:16],
            "evaluation_id": evaluation_id,
            "signal_id": signal["signal_id"],
            "human_score_percent": float(score) if score is not None else None,
            "criterion_scores": {str(key): float(value) for key, value in criterion_scores.items()},
            "human_verdict": signal.get("human_verdict"),
            "raw_text": signal.get("raw_text", ""),
            "review_context": signal.get("review_context"),
            "created_at": utc_now(),
        }
        existing = next(
            (
                item
                for item in self._repository.list_evaluation_overrides(evaluation_id=evaluation_id)
                if item.get("signal_id") == signal.get("signal_id")
            ),
            None,
        )
        if existing is not None:
            if {
                "human_score_percent": existing.get("human_score_percent"),
                "criterion_scores": existing.get("criterion_scores", {}),
                "human_verdict": existing.get("human_verdict"),
            } != {
                "human_score_percent": payload.get("human_score_percent"),
                "criterion_scores": payload.get("criterion_scores", {}),
                "human_verdict": payload.get("human_verdict"),
            }:
                raise ContractError(
                    f"signal {signal['signal_id']} was already applied with different values; "
                    "import a new signal_id to append a correction"
                )
            return {**existing, "reused": True}
        self._repository.append_evaluation_override(payload)
        return payload

    def effective_result(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        override = self._repository.latest_evaluation_override(evaluation["evaluation_id"])
        if override is None:
            return {
                **evaluation,
                "effective_case_score_percent": evaluation.get("case_score_percent"),
                "human_override": None,
            }
        result = {**evaluation}
        criterion_overrides = override.get("criterion_scores", {})
        if criterion_overrides:
            result = self._apply_criterion_scores(result, criterion_overrides)
        effective = override.get("human_score_percent")
        if effective is None:
            effective = result.get("scenario_score")
            if effective is None:
                effective = result.get("case_score_percent")
        return {
            **result,
            "effective_case_score_percent": effective,
            "human_override": override,
        }

    @staticmethod
    def _apply_criterion_scores(
        evaluation: dict[str, Any], criterion_overrides: dict[str, float]
    ) -> dict[str, Any]:
        criteria = []
        for item in evaluation.get("criterion_results", []):
            criterion_id = item.get("criterion_id")
            if criterion_id in criterion_overrides:
                score = float(criterion_overrides[criterion_id])
                criteria.append(
                    {
                        **item,
                        "score": score,
                        "score_percent": round(score / 2 * 100, 2),
                        "assessable": True,
                        "coverage_status": "human_override",
                    }
                )
            else:
                criteria.append(item)
        by_id = {item.get("criterion_id"): item for item in criteria}
        dimensions = []
        for dimension in evaluation.get("dimension_results", []):
            rows = [
                by_id.get(item.get("criterion_id"), item)
                for item in dimension.get("criterion_results", [])
            ]
            applicable = [item for item in rows if item.get("applicable", True)]
            scores = [
                float(item["score"])
                for item in applicable
                if isinstance(item.get("score"), (int, float))
            ]
            complete = bool(applicable) and len(scores) == len(applicable)
            score = round(sum(scores) / len(scores), 4) if complete else None
            dimensions.append(
                {
                    **dimension,
                    "criterion_results": rows,
                    "score": score,
                    "score_percent": (round(score / 2 * 100, 2) if score is not None else None),
                    "assessable": complete,
                    "coverage_complete": complete,
                }
            )
        dimension_by_id = {item.get("dimension_id"): item for item in dimensions}
        weight_resolution = evaluation.get("weight_resolution", {})
        scenario_score = _weighted(dimension_by_id, weight_resolution.get("effective_weights", {}))
        canonical_score = _weighted(dimension_by_id, weight_resolution.get("canonical_weights", {}))
        return {
            **evaluation,
            "criterion_results": criteria,
            "dimension_results": dimensions,
            "scenario_score": (
                scenario_score if scenario_score is not None else evaluation.get("scenario_score")
            ),
            "canonical_score": (
                canonical_score
                if canonical_score is not None
                else evaluation.get("canonical_score")
            ),
            "case_score_percent": (
                scenario_score
                if scenario_score is not None
                else evaluation.get("case_score_percent")
            ),
        }


def _weighted(dimensions: dict[str, dict[str, Any]], weights: dict[str, float]) -> float | None:
    if not weights:
        return None
    if any(
        not isinstance(dimensions.get(dimension_id, {}).get("score"), (int, float))
        for dimension_id in weights
    ):
        return None
    return round(
        sum(
            float(dimensions[dimension_id]["score"]) / 2 * float(weight)
            for dimension_id, weight in weights.items()
        )
        * 100,
        2,
    )
