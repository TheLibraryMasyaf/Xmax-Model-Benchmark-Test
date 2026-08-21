"""Judge worker: routes one dimension's evidence to its judges."""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from .registry import JudgeRegistry


class JudgeWorker:
    def __init__(self, registry: JudgeRegistry, artifact_store: Any = None) -> None:
        self._registry = registry
        self._artifacts = artifact_store

    def run(
        self,
        *,
        evaluation_id: str,
        run_id: str,
        benchmark_version: str,
        dimension_id: str,
        dimension_version: str,
        mode: str,
        context: dict[str, Any],
        judge_ids: list[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run all judges for one dimension; validates outputs."""

        judges = (
            [self._registry.get(judge_id, version) for judge_id, version in judge_ids]
            if judge_ids is not None
            else self._registry.for_dimension(dimension_id, mode)
        )
        if not judges:
            return [
                {
                    "dimension_id": dimension_id,
                    "status": "no_automated_judge",
                    "message": f"no compatible judge for {dimension_id}@{mode}",
                }
            ]
        results: list[dict[str, Any]] = []
        for registered in judges:
            base = {
                "evaluation_id": evaluation_id,
                "run_id": run_id,
                "benchmark_version": benchmark_version,
                "dimension_id": dimension_id,
                "dimension_version": dimension_version,
                "judge_id": registered.judge_id,
                "judge_version": registered.version,
            }
            try:
                judgments = registered.plugin.evaluate({**context, **base})
            except Exception as exc:
                criterion_results = [
                    {
                        "criterion_id": criterion_id,
                        "verdict": "judge_error",
                        "score": None,
                        "confidence": None,
                        "assessable": False,
                        "evidence": [{"description": str(exc)}],
                        "raw_metrics": {},
                    }
                    for criterion_id in sorted(
                        self._criterion_ids(context.get("dimension_contract") or {})
                    )
                ]
                results.append(
                    {
                        **base,
                        "verdict": "judge_error",
                        "score": None,
                        "confidence": None,
                        "assessable": False,
                        "error": str(exc),
                        "evidence": [{"description": str(exc)}],
                        "criterion_results": criterion_results,
                    }
                )
                continue
            for judgment in judgments:
                merged = self._normalize({**base, **judgment})
                self._validate(
                    merged,
                    registered.judge_id,
                    expected_criteria=self._criterion_ids(context.get("dimension_contract") or {}),
                )
                results.append(merged)
        return results

    def run_batch(
        self,
        *,
        evaluation_id: str,
        run_id: str,
        benchmark_version: str,
        mode: str,
        context: dict[str, Any],
        judge_id: str,
        judge_version: str,
        dimension_versions: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Run one batch-capable Judge once for multiple dimensions."""

        registered = self._registry.get(judge_id, judge_version)
        if not registered.manifest.get("batch_dimensions"):
            raise ValidationError(
                f"judge {judge_id}@{judge_version} does not support dimension batching"
            )
        base = {
            "evaluation_id": evaluation_id,
            "run_id": run_id,
            "benchmark_version": benchmark_version,
            "judge_id": judge_id,
            "judge_version": judge_version,
        }
        # A batch Judge owns all selected dimensions as one atomic Case
        # judgment. If it fails, propagate the error so the Case is retried in
        # full; never renormalize a score from the remaining CV/metric output.
        judgments = registered.plugin.evaluate({**context, **base})
        results = []
        seen: set[str] = set()
        for judgment in judgments:
            dimension_id = judgment.get("dimension_id")
            if dimension_id not in dimension_versions:
                raise ValidationError(
                    f"batch judge {judge_id} returned unexpected dimension {dimension_id!r}"
                )
            if dimension_id in seen:
                raise ValidationError(
                    f"batch judge {judge_id} returned duplicate dimension {dimension_id}"
                )
            seen.add(dimension_id)
            merged = self._normalize(
                {
                    **base,
                    "dimension_version": dimension_versions[dimension_id],
                    **judgment,
                }
            )
            contract = next(
                (
                    item
                    for item in context.get("dimension_contracts", [])
                    if item.get("dimension_id") == dimension_id
                ),
                {},
            )
            self._validate(
                merged,
                judge_id,
                expected_criteria=self._criterion_ids(contract),
                require_all_criteria=True,
            )
            results.append(merged)
        missing = set(dimension_versions) - seen
        if missing:
            raise ValidationError(f"batch judge {judge_id} omitted dimensions: {sorted(missing)}")
        return results

    @staticmethod
    def _criterion_ids(contract: dict[str, Any]) -> set[str]:
        return {
            item["criterion_id"]
            for item in contract.get("criteria", [])
            if item.get("criterion_id")
        }

    @staticmethod
    def _normalize(judgment: dict[str, Any]) -> dict[str, Any]:
        """Normalize a criterion-aware Judgment without inventing detail.

        The top-level score remains an audit convenience only. Fusion ignores it
        and always recomputes dimensions from ``criterion_results``.
        """

        if not judgment.get("criterion_results") and judgment.get("criterion_id"):
            judgment = {
                **judgment,
                "criterion_results": [
                    {
                        "criterion_id": judgment["criterion_id"],
                        "verdict": judgment.get("verdict", "unassessable"),
                        "score": judgment.get("score"),
                        "confidence": judgment.get("confidence"),
                        "assessable": judgment.get("assessable", judgment.get("score") is not None),
                        "evidence": list(judgment.get("evidence", [])),
                        "raw_metrics": dict(judgment.get("raw_metrics", {})),
                    }
                ],
            }
        criteria = list(judgment.get("criterion_results") or [])
        scores = [
            item.get("score")
            for item in criteria
            if item.get("assessable") is True and isinstance(item.get("score"), (int, float))
        ]
        return {
            **judgment,
            "score": round(sum(scores) / len(scores), 4) if scores else None,
            "assessable": bool(scores),
        }

    @staticmethod
    def _validate(
        judgment: dict[str, Any],
        judge_id: str,
        *,
        expected_criteria: set[str] | None = None,
        require_all_criteria: bool = False,
    ) -> None:
        required = (
            "dimension_id",
            "judge_id",
            "judge_version",
            "verdict",
            "evidence",
        )
        missing = [key for key in required if key not in judgment]
        if missing:
            raise ValidationError(f"judge {judge_id} output missing fields: {missing}")
        criteria = judgment.get("criterion_results")
        if not isinstance(criteria, list) or not criteria:
            raise ValidationError(
                f"judge {judge_id} must output non-empty criterion_results; "
                "dimension-only scores are not accepted"
            )
        seen: set[str] = set()
        for item in criteria:
            criterion_id = item.get("criterion_id")
            if not criterion_id:
                raise ValidationError(f"judge {judge_id} criterion result missing criterion_id")
            if criterion_id in seen:
                raise ValidationError(
                    f"judge {judge_id} returned duplicate criterion {criterion_id}"
                )
            seen.add(criterion_id)
            missing_fields = [
                key
                for key in (
                    "verdict",
                    "score",
                    "confidence",
                    "assessable",
                    "evidence",
                )
                if key not in item
            ]
            if missing_fields:
                raise ValidationError(
                    f"judge {judge_id} criterion {criterion_id} missing fields: {missing_fields}"
                )
            score = item.get("score")
            if item.get("assessable") is True and not isinstance(score, (int, float)):
                raise ValidationError(
                    f"judge {judge_id} criterion {criterion_id} is assessable but has no score"
                )
            if isinstance(score, (int, float)) and not 0 <= score <= 2:
                raise ValidationError(
                    f"judge {judge_id} criterion {criterion_id} score outside 0..2"
                )
        expected = expected_criteria or set()
        unexpected = seen - expected if expected else set()
        if unexpected:
            raise ValidationError(
                f"judge {judge_id} returned unexpected criteria: {sorted(unexpected)}"
            )
        if require_all_criteria and expected - seen:
            raise ValidationError(f"judge {judge_id} omitted criteria: {sorted(expected - seen)}")
