"""Judge worker: routes one dimension's evidence to its judges."""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from .base import JudgePlugin
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
                results.append(
                    {
                        **base,
                        "verdict": "judge_error",
                        "error": str(exc),
                        "evidence": [],
                    }
                )
                continue
            for judgment in judgments:
                merged = {**base, **judgment}
                self._validate(merged, registered.judge_id)
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
        try:
            judgments = registered.plugin.evaluate({**context, **base})
        except Exception as exc:
            return [
                {
                    **base,
                    "dimension_id": dimension_id,
                    "dimension_version": version,
                    "verdict": "judge_error",
                    "error": str(exc),
                    "evidence": [],
                }
                for dimension_id, version in dimension_versions.items()
            ]
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
            merged = {
                **base,
                "dimension_version": dimension_versions[dimension_id],
                **judgment,
            }
            self._validate(merged, judge_id)
            results.append(merged)
        missing = set(dimension_versions) - seen
        if missing:
            raise ValidationError(
                f"batch judge {judge_id} omitted dimensions: {sorted(missing)}"
            )
        return results

    @staticmethod
    def _validate(judgment: dict[str, Any], judge_id: str) -> None:
        required = (
            "dimension_id",
            "judge_id",
            "judge_version",
            "verdict",
            "evidence",
        )
        missing = [key for key in required if key not in judgment]
        if missing:
            raise ValidationError(
                f"judge {judge_id} output missing fields: {missing}"
            )
