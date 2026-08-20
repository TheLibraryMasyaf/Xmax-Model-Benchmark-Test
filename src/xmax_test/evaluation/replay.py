"""Historical replay.

Re-evaluates old GenerationRuns with a new Benchmark/Judge configuration. A
replay always creates a new Evaluation ID and never overwrites the old
evaluation; a new/old difference report is produced.
"""

from __future__ import annotations

from typing import Any

from ..hashing import content_hash
from ..time import utc_now


class ReplayService:
    def __init__(
        self, repository: Any, orchestrator: Any, artifacts: Any, clock: Any = None
    ) -> None:
        self._repository = repository
        self._orchestrator = orchestrator
        self._artifacts = artifacts
        self._clock = clock

    def replay_runs(self, runs: list[dict[str, Any]]) -> dict[str, Any]:
        """Re-evaluate runs; old results are preserved under their IDs."""

        new_summary = self._orchestrator.evaluate_runs(runs)
        differences: list[dict[str, Any]] = []
        for new_result in new_summary.get("results", []):
            old_results = [
                item
                for item in self._repository.list_evaluation_results(
                    run_id=new_result.get("run_id")
                )
                if item.get("evaluation_id") != new_result.get("evaluation_id")
            ]
            if not old_results:
                continue
            old = old_results[0]
            differences.append(
                {
                    "run_id": new_result.get("run_id"),
                    "old_evaluation_id": old.get("evaluation_id"),
                    "new_evaluation_id": new_result.get("evaluation_id"),
                    "old_benchmark_version": old.get("benchmark_version"),
                    "new_benchmark_version": new_result.get("benchmark_version"),
                    "old_canonical_score": old.get("canonical_score"),
                    "new_canonical_score": new_result.get("canonical_score"),
                    "old_scenario_score": old.get("scenario_score"),
                    "new_scenario_score": new_result.get("scenario_score"),
                    "delta_canonical": _delta(old.get("canonical_score"), new_result.get("canonical_score")),
                    "delta_scenario": _delta(old.get("scenario_score"), new_result.get("scenario_score")),
                }
            )
        return {
            "replayed_evaluations": len(new_summary.get("results", [])),
            "old_results_preserved": True,
            "differences": differences,
            "evaluation_batch_id": new_summary.get("evaluation_batch_id"),
        }


def _delta(old: Any, new: Any) -> float | None:
    if old is None or new is None:
        return None
    return round(float(new) - float(old), 2)
