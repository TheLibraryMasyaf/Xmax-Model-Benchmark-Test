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
from .weights import resolve_scene_weights


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

        criterion_scores = self._merge_criteria(benchmark, judgments, mode)
        dimension_scores = self._derive_dimensions(benchmark, criterion_scores, mode)
        gate_result = self._gates.evaluate(
            benchmark, dimension_scores, runtime_facts, criterion_scores
        )
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
            "criterion_results": self._criterion_results(criterion_scores),
            "dimension_results": self._dimension_results(dimension_scores),
            "weight_resolution": self._weight_resolution_payload(canonical, scenario, test_case),
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
                "dimension_count": len(dimension_scores),
                "assessable_criteria": sorted(
                    criterion_id
                    for criterion_id, item in criterion_scores.items()
                    if item.get("assessable")
                ),
                "unassessable_criteria": sorted(
                    criterion_id
                    for criterion_id, item in criterion_scores.items()
                    if item.get("coverage_status") == "unassessable"
                ),
                "uncovered_criteria": sorted(
                    criterion_id
                    for criterion_id, item in criterion_scores.items()
                    if item.get("coverage_status") == "uncovered"
                ),
                "criterion_count": len(criterion_scores),
                "canonical_missing_dimensions": canonical.get("missing_dimensions", []),
                "scenario_missing_dimensions": scenario.get("missing_dimensions", []),
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
                canonical_resolution.base_profile_id if canonical_resolution is not None else None
            ),
            "canonical_weights": (
                canonical_resolution.effective_weights if canonical_resolution is not None else {}
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
                "score_percent": item.get("score_percent"),
                "assessable": item.get("assessable", False),
                "applicable": item.get("applicable", True),
                "judges": item.get("judges", []),
                "judge_versions": item.get("judge_versions", []),
                "confidence": item.get("confidence"),
                "evidence": item.get("evidence", []),
                "criterion_count": item.get("criterion_count", 0),
                "assessable_criterion_count": item.get("assessable_criterion_count", 0),
                "coverage_complete": item.get("coverage_complete", False),
                "criterion_results": item.get("criterion_results", []),
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

    @staticmethod
    def _criterion_results(
        criterion_scores: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [criterion_scores[item] for item in sorted(criterion_scores)]

    def _merge_criteria(
        self,
        benchmark: dict[str, Any],
        judgments: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, dict[str, Any]]:
        """Fuse Judge output at criterion level; dimension scores are ignored."""

        contracts: dict[str, tuple[str, str]] = {}
        for dimension in benchmark.get("dimensions", []):
            modes = dimension.get("applicable_modes", [])
            if mode not in modes and "both" not in modes:
                continue
            for criterion in dimension.get("criteria", []):
                criterion_id = criterion.get("criterion_id")
                if criterion_id:
                    contracts[criterion_id] = (
                        dimension["dimension_id"],
                        criterion.get("name", ""),
                    )

        grouped: dict[str, list[dict[str, Any]]] = {}
        submitted: set[str] = set()
        for judgment in judgments:
            if judgment.get("status") == "no_automated_judge":
                continue
            entries = list(judgment.get("criterion_results") or [])
            # Read-only compatibility for old specialized records that already
            # named a criterion. Dimension-only legacy scores are never spread
            # across criteria.
            if not entries and judgment.get("criterion_id"):
                entries = [{**judgment, "criterion_id": judgment["criterion_id"]}]
            for entry in entries:
                criterion_id = entry.get("criterion_id")
                if criterion_id not in contracts:
                    continue
                submitted.add(criterion_id)
                grouped.setdefault(criterion_id, []).append(
                    {
                        **entry,
                        "judge_id": judgment.get("judge_id"),
                        "judge_version": judgment.get("judge_version"),
                    }
                )

        merged: dict[str, dict[str, Any]] = {}
        for criterion_id, (dimension_id, criterion_name) in contracts.items():
            items = grouped.get(criterion_id, [])
            scored = [
                item
                for item in items
                if item.get("assessable") is True and isinstance(item.get("score"), (int, float))
            ]
            scores = [float(item["score"]) for item in scored]
            explicitly_not_applicable = bool(items) and all(
                item.get("applicable") is False for item in items
            )
            score = _mean(scores)
            coverage_status = (
                "not_applicable"
                if explicitly_not_applicable
                else (
                    "assessed"
                    if scores
                    else ("unassessable" if criterion_id in submitted else "uncovered")
                )
            )
            evidence = [
                {
                    **entry,
                    "judge_id": item.get("judge_id"),
                    "judge_version": item.get("judge_version"),
                }
                for item in items
                for entry in item.get("evidence", [])
            ]
            judges = sorted({str(item.get("judge_id")) for item in items if item.get("judge_id")})
            judge_versions = sorted(
                {
                    f"{item.get('judge_id')}@{item.get('judge_version')}"
                    for item in items
                    if item.get("judge_id")
                }
            )
            merged[criterion_id] = {
                "dimension_id": dimension_id,
                "criterion_id": criterion_id,
                "criterion_name": criterion_name,
                "score": score,
                "score_percent": round(score / 2 * 100, 2) if score is not None else None,
                "assessable": bool(scores),
                "applicable": not explicitly_not_applicable,
                "coverage_status": coverage_status,
                "judges": judges,
                "judge_versions": judge_versions,
                "confidence": _mean(
                    [
                        float(item["confidence"])
                        for item in scored
                        if isinstance(item.get("confidence"), (int, float))
                    ]
                ),
                "evidence": evidence,
                "judge_score_count": len(scores),
            }
        return merged

    def _derive_dimensions(
        self,
        benchmark: dict[str, Any],
        criterion_scores: dict[str, dict[str, Any]],
        mode: str,
    ) -> dict[str, dict[str, Any]]:
        """Deterministically derive each dimension from applicable criteria."""

        results: dict[str, dict[str, Any]] = {}
        for dimension in benchmark.get("dimensions", []):
            modes = dimension.get("applicable_modes", [])
            if mode not in modes and "both" not in modes:
                continue
            criteria = [
                criterion_scores[item["criterion_id"]]
                for item in dimension.get("criteria", [])
                if item.get("criterion_id") in criterion_scores
            ]
            applicable_criteria = [item for item in criteria if item.get("applicable", True)]
            assessable = [
                item for item in applicable_criteria if isinstance(item.get("score"), (int, float))
            ]
            dimension_applicable = bool(applicable_criteria)
            coverage_complete = dimension_applicable and len(assessable) == len(applicable_criteria)
            # A missing or unassessable criterion is not an N/A exclusion. A
            # dimension may only receive its full profile weight when every
            # applicable criterion was assessed. This prevents one easy
            # criterion from being inflated to represent the entire dimension.
            score = (
                _mean([float(item["score"]) for item in assessable]) if coverage_complete else None
            )
            results[dimension["dimension_id"]] = {
                "dimension_id": dimension["dimension_id"],
                "score": score,
                "score_percent": round(score / 2 * 100, 2) if score is not None else None,
                "assessable": coverage_complete,
                "applicable": dimension_applicable,
                "coverage_complete": coverage_complete,
                "judges": sorted({judge for item in criteria for judge in item.get("judges", [])}),
                "judge_versions": sorted(
                    {version for item in criteria for version in item.get("judge_versions", [])}
                ),
                "confidence": _mean(
                    [
                        float(item["confidence"])
                        for item in assessable
                        if isinstance(item.get("confidence"), (int, float))
                    ]
                ),
                "evidence": [entry for item in criteria for entry in item.get("evidence", [])],
                "criterion_count": len(applicable_criteria),
                "assessable_criterion_count": len(assessable),
                "criterion_results": criteria,
            }
        return results

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
        resolution = resolve_scene_weights(
            profile,
            rules,
            mode=mode,
            scenario_id=scenario_id,
            scene_tags=scene_tags,
            # Preserve the approved profile denominator. Only explicit scene
            # rule exclusions are removed; missing Judge coverage never causes
            # the remaining dimensions to be renormalized to 100%.
            assessable_dimensions={
                dimension_id
                for dimension_id in profile.get("weights", {})
                if dimension_scores.get(dimension_id, {}).get("applicable", True)
            },
            include_shadow_rules=include_shadow,
        )
        missing_dimensions = sorted(set(resolution.effective_weights) - assessable)
        if missing_dimensions:
            return {
                "score": None,
                "resolution": resolution,
                "missing_dimensions": missing_dimensions,
            }
        total = 0.0
        for dimension_id, weight in resolution.effective_weights.items():
            item = dimension_scores.get(dimension_id, {})
            score = item.get("score")
            if isinstance(score, (int, float)):
                total += (score / 2.0) * weight
        score = round(total * 100, 2) if resolution.effective_weights else None
        return {
            "score": score,
            "resolution": resolution,
            "missing_dimensions": [],
        }


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)
