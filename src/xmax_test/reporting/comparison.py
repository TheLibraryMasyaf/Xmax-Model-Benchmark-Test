"""Model version comparison.

Pairs baseline and candidate runs on the same Feed/Prompt/scene/mode/repeat,
checks comparability, and aggregates canonical/scenario deltas per scene and
overall. Only ``model_version`` may differ; anything else makes the report
``not_comparable`` and forbids up/down conclusions.
"""

from __future__ import annotations

import statistics
from typing import Any

from ..hashing import content_hash


class ModelComparisonService:
    def __init__(
        self,
        repository: Any,
        benchmark: dict[str, Any],
        scenario_pack: dict[str, Any],
        score_schema: dict[str, Any],
    ) -> None:
        self._repository = repository
        self._benchmark = benchmark
        self._scenario_pack = scenario_pack
        self._score_schema = score_schema

    def compare(
        self,
        *,
        baseline_model_version: str,
        candidate_model_version: str,
        requested_scene_ids: list[str],
    ) -> dict[str, Any]:
        baseline_runs = self._runs_for_model(baseline_model_version)
        candidate_runs = self._runs_for_model(candidate_model_version)

        if not baseline_runs or not candidate_runs:
            return {
                "status": "not_comparable",
                "comparability": {
                    "comparable": False,
                    "differences": [
                        {
                            "kind": "missing_runs",
                            "baseline": len(baseline_runs),
                            "candidate": len(candidate_runs),
                        }
                    ],
                    "excluded_case_ids": [],
                },
                "pairs": [],
            }

        baseline_by_key = self._keyed(baseline_runs, baseline_model_version)
        candidate_by_key = self._keyed(candidate_runs, candidate_model_version)
        shared_keys = sorted(set(baseline_by_key) & set(candidate_by_key))
        excluded = sorted(set(baseline_by_key) ^ set(candidate_by_key))

        differences = self._comparability_differences(
            baseline_by_key, candidate_by_key, baseline_model_version, candidate_model_version
        )
        if excluded:
            differences.append(
                {
                    "kind": "unpaired_cases",
                    "count": len(excluded),
                    "keys": ["|".join(item) for item in excluded],
                }
            )
        comparable = not differences

        pairs: list[dict[str, Any]] = []
        for key in shared_keys:
            baseline = baseline_by_key[key]
            candidate = candidate_by_key[key]
            if self._keys_comparable(baseline, candidate):
                pairs.append(
                    {
                        "key": key,
                        "case_number": baseline.get("case_number", ""),
                        "scene_id": key[1],
                        "mode": key[2],
                        "baseline": baseline,
                        "candidate": candidate,
                    }
                )
        if not comparable and not pairs:
            return {
                "comparison_id": "",
                "status": "not_comparable",
                "comparability": {
                    "comparable": False,
                    "differences": differences,
                    "excluded_case_ids": excluded,
                },
                "pairs": [],
            }

        overall = self._aggregate(pairs)
        scene_results = [
            self._scene_result(pairs, scene_id) for scene_id in requested_scene_ids
        ]
        return {
            "status": "partial" if not comparable else "complete",
            "comparability": {
                "comparable": comparable,
                "differences": differences,
                "excluded_case_ids": excluded,
            },
            "pairs": pairs,
            "overall": overall,
            "scene_results": scene_results,
            "generation_config_hash": self._common_generation_config_hash(pairs),
            "judge_versions": sorted(
                {
                    version
                    for pair in pairs
                    for side in ("baseline", "candidate")
                    for version in _judge_versions(pair[side].get("_evaluation", {}))
                }
            ),
        }

    # ------------------------------------------------------------------
    def _runs_for_model(self, model_version: str) -> list[dict[str, Any]]:
        runs = []
        for run in self._repository.list_runs(model_id=model_version):
            result = self._repository.latest_evaluation_result(
                run.get("run_id"), self._benchmark.get("benchmark_version")
            )
            if result is None:
                if run.get("status") == "error":
                    run["_evaluation"] = {
                        "case_score_percent": 0.0,
                        "canonical_score": None,
                        "scenario_score": None,
                        "applied_gate_ids": ["generation-failed"],
                        "final_verdict": "generation_failed",
                    }
                else:
                    continue
            else:
                run["_evaluation"] = result
            runs.append(run)
        return runs

    def _keyed(self, runs: list[dict[str, Any]], model_version: str) -> dict[tuple[str, str, str], dict[str, Any]]:
        keyed: dict[tuple[str, str, str], dict[str, Any]] = {}
        for run in runs:
            case = self._case_of(run)
            key = (
                self._pairing_key(case),
                case.get("scenario_id") or "",
                run.get("mode", "offline"),
            )
            # Repeat groups: later repeats keep distinct keys per repeat index.
            keyed[key] = run
        return keyed

    @staticmethod
    def _pairing_key(case: dict[str, Any]) -> str:
        return content_hash(
            {
                "feed_asset_id": case.get("feed_asset_id"),
                "prompt_asset_ids": case.get("prompt_asset_ids", []),
                "prompt_text": case.get("prompt_text", ""),
                "operation_recipe_id": case.get("operation_recipe_id"),
                "operation_recipe_version": case.get("operation_recipe_version"),
                "generation_mode": case.get("generation_mode"),
                "repeat_index": case.get("repeat_index"),
                "scenario_id": case.get("scenario_id"),
                "scene_tags": case.get("scene_tags", {}),
                "api_asset_bindings": case.get("api_asset_bindings", {}),
                "generation_config": case.get("generation_config", {}),
            }
        )

    def _case_of(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._repository.get_test_case(run.get("case_id", ""))

    def _comparability_differences(
        self,
        baseline: dict[tuple[str, str, str], dict[str, Any]],
        candidate: dict[tuple[str, str, str], dict[str, Any]],
        baseline_model: str,
        candidate_model: str,
    ) -> list[dict[str, Any]]:
        differences: list[dict[str, Any]] = []
        if self._benchmark.get("status") not in {"shadow", "active"}:
            differences.append({"kind": "benchmark_status", "value": self._benchmark.get("status")})
        for key in sorted(set(baseline) & set(candidate)):
            left = baseline[key]
            right = candidate[key]
            left_eval = left.get("_evaluation", {})
            right_eval = right.get("_evaluation", {})
            for field, left_value, right_value in (
                ("mode", left.get("mode"), right.get("mode")),
                ("benchmark_version", left_eval.get("benchmark_version"), right_eval.get("benchmark_version")),
                ("score_schema_version", left_eval.get("score_schema_version"), right_eval.get("score_schema_version")),
                ("scenario_pack_version", left_eval.get("scenario_pack_version"), right_eval.get("scenario_pack_version")),
                ("preprocessor_version", left_eval.get("preprocessor_version"), right_eval.get("preprocessor_version")),
                (
                    "generation_config_hash",
                    content_hash(self._case_of(left).get("generation_config", {})),
                    content_hash(self._case_of(right).get("generation_config", {})),
                ),
                ("judge_versions", _judge_versions(left_eval), _judge_versions(right_eval)),
            ):
                if left_value != right_value:
                    differences.append(
                        {"kind": field, "case": left.get("case_number"), "baseline": left_value, "candidate": right_value}
                    )
        return differences

    def _common_generation_config_hash(
        self, pairs: list[dict[str, Any]]
    ) -> str | None:
        hashes = {
            content_hash(self._case_of(pair[side]).get("generation_config", {}))
            for pair in pairs
            for side in ("baseline", "candidate")
        }
        return next(iter(hashes)) if len(hashes) == 1 else None

    def _keys_comparable(self, baseline: dict[str, Any], candidate: dict[str, Any]) -> bool:
        return baseline.get("mode") == candidate.get("mode")

    def _aggregate(self, pairs: list[dict[str, Any]]) -> dict[str, Any]:
        baseline_scores = [self._case_score(p["baseline"]) for p in pairs]
        candidate_scores = [self._case_score(p["candidate"]) for p in pairs]
        return {
            "canonical": self._delta_summary(pairs, "canonical_score"),
            "scenario": self._delta_summary(pairs, "scenario_score"),
            "dimensions": self._dimension_summaries(pairs),
            "baseline_mean_percent": _mean(baseline_scores),
            "candidate_mean_percent": _mean(candidate_scores),
            "comparable_pairs": len(pairs),
            "baseline_stats": _stats(baseline_scores),
            "candidate_stats": _stats(candidate_scores),
        }

    def _dimension_summaries(self, pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        dimension_ids = sorted(
            {
                item.get("dimension_id")
                for pair in pairs
                for side in ("baseline", "candidate")
                for item in pair[side].get("_evaluation", {}).get("dimension_results", [])
                if item.get("dimension_id")
            }
        )
        summaries = []
        for dimension_id in dimension_ids:
            baseline = _mean(
                _dimension_score(pair["baseline"], dimension_id) for pair in pairs
            )
            candidate = _mean(
                _dimension_score(pair["candidate"], dimension_id) for pair in pairs
            )
            # Dimension scores use 0..2; report them as percentages.
            baseline_percent = round(baseline / 2 * 100, 2) if baseline is not None else None
            candidate_percent = round(candidate / 2 * 100, 2) if candidate is not None else None
            summaries.append(
                {
                    "dimension_id": dimension_id,
                    "baseline": baseline_percent,
                    "candidate": candidate_percent,
                    "delta_points": _delta(baseline_percent, candidate_percent),
                }
            )
        return summaries

    def _delta_summary(self, pairs: list[dict[str, Any]], field: str) -> dict[str, Any]:
        baseline_values = [
            p["baseline"].get("_evaluation", {}).get(field) for p in pairs
        ]
        candidate_values = [
            p["candidate"].get("_evaluation", {}).get(field) for p in pairs
        ]
        baseline = _mean(baseline_values)
        candidate = _mean(candidate_values)
        return {
            "baseline": baseline,
            "candidate": candidate,
            "delta_points": round(candidate - baseline, 2) if baseline is not None and candidate is not None else None,
            "delta_percent": (
                round((candidate - baseline) / baseline * 100, 2)
                if baseline not in (None, 0) and candidate is not None
                else None
            ),
            "classification": "unclassified",
        }

    def _scene_result(self, pairs: list[dict[str, Any]], scene_id: str) -> dict[str, Any]:
        scene_pairs = [p for p in pairs if p["scene_id"] == scene_id]
        modes = sorted({p["mode"] for p in scene_pairs})
        baseline_scores = [self._case_score(p["baseline"]) for p in scene_pairs]
        candidate_scores = [self._case_score(p["candidate"]) for p in scene_pairs]
        return {
            "scenario_id": scene_id,
            "mode": modes[0] if len(modes) == 1 else "offline",
            "score_summary": self._delta_summary(scene_pairs, "scenario_score") if scene_pairs else {
                "baseline": None,
                "candidate": None,
                "delta_points": None,
                "delta_percent": None,
                "classification": "not_comparable",
            },
            "items": [
                {
                    "scope": "scene",
                    "item_id": pair["case_number"],
                    "scenario_id": scene_id,
                    "baseline": self._case_score(pair["baseline"]),
                    "candidate": self._case_score(pair["candidate"]),
                    "delta_points": _delta(
                        self._case_score(pair["baseline"]),
                        self._case_score(pair["candidate"]),
                    ),
                    "evidence_ids": [
                        run_id
                        for run_id in (
                            pair["baseline"].get("run_id"),
                            pair["candidate"].get("run_id"),
                        )
                        if run_id
                    ],
                    "baseline_run_id": pair["baseline"].get("run_id"),
                    "candidate_run_id": pair["candidate"].get("run_id"),
                }
                for pair in scene_pairs
            ],
            "evidence_ids": [
                run_id
                for pair in scene_pairs
                for run_id in (pair["baseline"].get("run_id"), pair["candidate"].get("run_id"))
                if run_id
            ],
            "comparable_pairs": len(scene_pairs),
            "baseline_stats": _stats(baseline_scores),
            "candidate_stats": _stats(candidate_scores),
            "hard_gate_failures": {
                "baseline": sum(
                    1 for pair in scene_pairs if pair["baseline"].get("_evaluation", {}).get("applied_gate_ids")
                ),
                "candidate": sum(
                    1 for pair in scene_pairs if pair["candidate"].get("_evaluation", {}).get("applied_gate_ids")
                ),
            },
        }

    @staticmethod
    def _case_score(run: dict[str, Any]) -> float | None:
        value = run.get("_evaluation", {}).get("case_score_percent")
        if value is None:
            return None
        if run.get("status") != "completed":
            return 0.0
        return float(value)


def _mean(values: list[Any]) -> float | None:
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return None
    return round(statistics.fmean(numbers), 2)


def _stats(values: list[Any]) -> dict[str, Any]:
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return {"n": 0}
    return {
        "n": len(numbers),
        "case_scores_percent": [round(number, 2) for number in numbers],
        "mean": round(statistics.fmean(numbers), 2),
        "median": round(statistics.median(numbers), 2),
        "min": round(min(numbers), 2),
        "max": round(max(numbers), 2),
        "stdev": round(statistics.pstdev(numbers), 2) if len(numbers) > 1 else 0.0,
        "p25": round(sorted(numbers)[max(0, len(numbers) // 4 - 1)], 2),
        "p75": round(sorted(numbers)[min(len(numbers) - 1, 3 * len(numbers) // 4 - 1)], 2),
        "success_rate": round(sum(1 for n in numbers if n > 0) / len(numbers), 3),
    }


def _delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(right - left, 2)


def _dimension_score(run: dict[str, Any], dimension_id: str) -> float | None:
    for item in run.get("_evaluation", {}).get("dimension_results", []):
        if item.get("dimension_id") == dimension_id:
            value = item.get("score")
            return float(value) if value is not None else None
    return None


def _judge_versions(evaluation: dict[str, Any]) -> list[str]:
    return sorted(
        {
            version
            for item in evaluation.get("dimension_results", [])
            for version in item.get("judge_versions", [])
        }
    )
