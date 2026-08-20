"""Release gates for benchmark/score-schema versions.

A failed version cannot be promoted; the previous Champion is retained for
rollback. Release decisions require Holdout evidence.
"""

from __future__ import annotations

from typing import Any

from ..errors import ContractError


class ReleaseService:
    def __init__(
        self, repository: Any, benchmark: dict[str, Any], score_schema: dict[str, Any]
    ) -> None:
        self._repository = repository
        self._benchmark = benchmark
        self._score_schema = score_schema

    def validate(self, *, holdout_results: list[dict[str, Any]], threshold: float = 0.5) -> dict[str, Any]:
        """Validate a candidate release against holdout results."""

        benchmark_version = self._benchmark.get("benchmark_version")
        schema_version = self._score_schema.get("version")
        if not holdout_results:
            raise ContractError(
                "release validation requires holdout results; holdout must be "
                "isolated from training"
            )
        # Any blocking hard gate failure in holdout blocks promotion.
        gate_failures = [
            r for r in holdout_results if r.get("applied_gate_ids") and r.get("final_verdict")
        ]
        valid = len(gate_failures) == 0
        return {
            "benchmark_version": benchmark_version,
            "score_schema_version": schema_version,
            "holdout_sample_count": len(holdout_results),
            "hard_gate_failures": len(gate_failures),
            "valid": valid,
            "threshold": threshold,
        }

    def promote(self, validation: dict[str, Any], operator: str) -> dict[str, Any]:
        if not validation.get("valid"):
            raise ContractError("cannot promote a failed release version")
        self._repository.record_judge_release(
            "benchmark",
            self._benchmark.get("benchmark_version"),
            "champion",
            {
                "score_schema_version": self._score_schema.get("version"),
                "operator": operator,
                "holdout_sample_count": validation.get("holdout_sample_count"),
                "promoted_at": None,
            },
        )
        return {"status": "promoted", "benchmark_version": self._benchmark.get("benchmark_version")}

    def rollback(self, previous_version: str, operator: str) -> dict[str, Any]:
        """Return the previous Champion and record the rollback event."""

        self._repository.record_judge_release(
            "benchmark",
            previous_version,
            "champion",
            {"operator": operator, "rollback": True},
        )
        return {"status": "rolled_back", "benchmark_version": previous_version}
