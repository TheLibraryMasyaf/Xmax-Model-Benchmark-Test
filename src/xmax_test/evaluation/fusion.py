"""Judgment fusion and scoring.

Computes the canonical score (fixed profile, cross-scene comparable) and the
scenario score (preset scene rules) side by side, records matched rules and
effective weights, and applies hard gates before any weighted total.
"""

from __future__ import annotations

from typing import Any

from ..errors import ContractError
from ..hashing import content_hash
from .gates import HardGateEvaluator
from .weights import resolve_scene_weights, WeightResolution


class JudgmentFusion:
    def __init__(self, gates: HardGateEvaluator | None = None) -> None:
        self._gates = gates or HardGateEvaluator()

    def fuse(
        self,
        benchmark: dict[str, Any],
        scenario_pack: dict[str, Any],
        test_case: dict[str, Any],
        judgments: list[dict[str, Any]],
        runtime_facts: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a complete evaluation result with versioned weight tracks."""

        mode = test_case.get("generation_mode", "offline")
        benchmark_version = benchmark.get("benchmark_version", "")
        score_schema = self._score_schema(benchmark, mode)
        profile = self._profile_for_mode(benchmark, mode)

        dimension_scores = self._merge_dimensions(judgments)
        gate_result = self._gates.evaluate(benchmark, dimension_scores, runtime_facts)
        assessable = {
            dimension_id
            for dimension_id, item in dimension_scores.items()
            if item.get("assessable", item.get("score") is not None)
        }

        canonical = self._weighted_score(
            profile,
            [],
            dimension_scores,
            assessable,
            mode=mode,
            scenario_id=None,
            scene_tags={},
            include_shadow=False,
        )
        scenario = self._weighted_score(
            profile,
            benchmark.get("scene_weight_rules", []),
            dimension_scores,
            assessable,
            mode=mode,
            scenario_id=test_case.get("scenario_id"),
            scene_tags=test_case.get("scene_tags", {}),
            include_shadow=True,
        )

        case_score = None
        if gate_result["block_score"]:
            case_score = 0.0
        else:
            output = score_schema.get("case_score_output", "scenario_score")
            value = scenario["score"] if output == "scenario_score" else canonical["score"]
            if value is not None:
                case_score = round(value, 2)  # value is already on the 0-100 scale

        return {
            "benchmark_version": benchmark_version,
            "scenario_pack_version": scenario_pack.get("version", ""),
            "score_schema_version": score_schema.get("version", ""),
            "canonical_score": canonical["score"],
            "scenario_score": scenario["score"],
            "case_score_percent": case_score,
            "score_display_format": "percentage",
            "dimension_results": self._dimension_results(dimension_scores),
            "weight_resolution": self._weight_resolution_payload(
                canonical, scenario, test_case
            ),
            "applied_gate_ids": gate_result["applied_gate_ids"],
            "final_verdict": gate_result["final_verdict"],
            "no_automated_judge_dimensions": sorted(
                {
                    item.get("dimension_id", "")
                    for item in judgments
                    if item.get("status") == "no_automated_judge"
                }
            ),
            "coverage": {
                "assessable_dimensions": sorted(assessable),
                "judged_dimensions": sorted(dimension_scores),
                "dimension_count": len(benchmark.get("dimensions", [])),
            },
        }

    @staticmethod
    def _weight_resolution_payload(
        canonical: dict[str, Any], scenario: dict[str, Any], test_case: dict[str, Any]
    ) -> dict[str, Any]:
        canonical_resolution = canonical["resolution"]
        scenario_resolution = scenario["resolution"]
        empty: dict[str, Any] = {
            "base_profile_id": "",
            "base_profile_version": "",
            "matched_rule_ids": [],
            "matched_rule_versions": [],
            "scene_tags_hash": content_hash(test_case.get("scene_tags", {})),
            "effective_weights": {},
            "excluded_dimensions": [],
            "rule_excluded_dimensions": [],
        }
        if scenario_resolution is None and canonical_resolution is None:
            return empty
        if scenario_resolution is None:
            return {
                **empty,
                "base_profile_id": canonical_resolution.base_profile_id,
                "base_profile_version": canonical_resolution.base_profile_version,
                "effective_weights": canonical_resolution.effective_weights,
            }
        return {
            "base_profile_id": scenario_resolution.base_profile_id,
            "base_profile_version": scenario_resolution.base_profile_version,
            "matched_rule_ids": list(scenario_resolution.matched_rule_ids),
            "matched_rule_versions": list(scenario_resolution.matched_rule_versions),
            "scene_tags_hash": content_hash(test_case.get("scene_tags", {})),
            "effective_weights": scenario_resolution.effective_weights,
            "excluded_dimensions": list(scenario_resolution.excluded_dimensions),
            "rule_excluded_dimensions": list(scenario_resolution.rule_excluded_dimensions),
            "canonical_profile_id": (
                canonical_resolution.base_profile_id
                if canonical_resolution is not None
                else None
            ),
            "canonical_weights": (
                canonical_resolution.effective_weights
                if canonical_resolution is not None
                else {}
            ),
        }

    # ------------------------------------------------------------------
    def _dimension_results(
        self, dimension_scores: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "dimension_id": dimension_id,
                "score": item.get("score"),
                "assessable": item.get("assessable", False),
                "judges": item.get("judges", []),
                "judge_versions": item.get("judge_versions", []),
                "confidence": item.get("confidence"),
                "evidence": item.get("evidence", []),
            }
            for dimension_id, item in sorted(dimension_scores.items())
        ]

    def _score_schema(self, benchmark: dict[str, Any], mode: str) -> dict[str, Any]:
        schemas = [
            item
            for item in benchmark.get("score_schemas", [])
            if item.get("status") in {"active", "shadow", "draft"}
        ]
        if not schemas:
            raise ContractError("benchmark has no executable score schema")
        return schemas[0]

    def _profile_for_mode(self, benchmark: dict[str, Any], mode: str) -> dict[str, Any]:
        schema = self._score_schema(benchmark, mode)
        profile_id = schema.get("weight_profile_by_mode", {}).get(mode)
        for profile in benchmark.get("weight_profiles", []):
            if profile.get("profile_id") == profile_id:
                return profile
        raise ContractError(f"no weight profile {profile_id!r} for mode {mode}")

    def _merge_dimensions(
        self, judgments: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Merge multi-judge outputs per dimension (primary first, then mean)."""

        grouped: dict[str, list[dict[str, Any]]] = {}
        for judgment in judgments:
            dimension_id = judgment.get("dimension_id")
            if not dimension_id or judgment.get("status") == "no_automated_judge":
                continue
            grouped.setdefault(dimension_id, []).append(judgment)
        merged: dict[str, dict[str, Any]] = {}
        for dimension_id, items in grouped.items():
            scores = [
                item["score"] for item in items if isinstance(item.get("score"), (int, float))
            ]
            merged[dimension_id] = {
                "dimension_id": dimension_id,
                "score": sum(scores) / len(scores) if scores else None,
                "assessable": bool(scores),
                "judges": [item.get("judge_id") for item in items],
                "judge_versions": sorted(
                    {
                        f"{item.get('judge_id')}@{item.get('judge_version')}"
                        for item in items
                    }
                ),
                "confidence": _mean(
                    [
                        item["confidence"]
                        for item in items
                        if isinstance(item.get("confidence"), (int, float))
                    ]
                ),
                "evidence": [e for item in items for e in item.get("evidence", [])],
            }
        return merged

    def _weighted_score(
        self,
        profile: dict[str, Any],
        rules: list[dict[str, Any]],
        dimension_scores: dict[str, dict[str, Any]],
        assessable: set[str],
        *,
        mode: str,
        scenario_id: str | None,
        scene_tags: dict[str, Any],
        include_shadow: bool,
    ) -> dict[str, Any]:
        assessable_intersection = assessable & set(profile.get("weights", {}))
        if not assessable_intersection:
            # No assessable dimensions: do not fabricate a misleading total.
            return {"score": None, "resolution": None}
        resolution = resolve_scene_weights(
            profile,
            rules,
            mode=mode,
            scenario_id=scenario_id,
            scene_tags=scene_tags,
            assessable_dimensions=assessable_intersection,
            include_shadow_rules=include_shadow,
        )
        total = 0.0
        for dimension_id, weight in resolution.effective_weights.items():
            item = dimension_scores.get(dimension_id, {})
            score = item.get("score")
            if isinstance(score, (int, float)):
                total += (score / 2.0) * weight
        score = round(total * 100, 2) if resolution.effective_weights else None
        return {"score": score, "resolution": resolution}


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)
