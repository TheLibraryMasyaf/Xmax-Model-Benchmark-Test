"""P0/P1/P2 classification.

Classification thresholds must come from the published Score Schema's
``comparison_policy``. When thresholds are missing, only numeric changes are
reported and no classification is invented. New Hard Gate failures always
enter P2.
"""

from __future__ import annotations

from typing import Any

CLASSIFICATION_ORDER = {"p0": 0, "p1": 1, "p2": 2}


def comparison_policy(score_schema: dict[str, Any]) -> dict[str, Any] | None:
    policy = score_schema.get("comparison_policy")
    if isinstance(policy, dict) and "improvement_min_delta" in policy:
        return policy
    # "pending_first_round" has no thresholds yet.
    return None


def classify_delta(delta: float | None, policy: dict[str, Any] | None) -> str:
    if policy is None or delta is None:
        return "unclassified"
    if delta >= policy["improvement_min_delta"]:
        return "p0"
    if abs(delta) <= policy["tie_abs_delta_max"]:
        return "p1"
    if delta <= -policy["regression_min_delta"]:
        return "p2"
    return "p1"


def classify_pair(pair: dict[str, Any], policy: dict[str, Any] | None) -> dict[str, Any]:
    """Classify one paired sample on both canonical and scenario deltas."""

    baseline_eval = pair["baseline"].get("_evaluation", {})
    candidate_eval = pair["candidate"].get("_evaluation", {})
    result: dict[str, Any] = {
        "case_number": pair.get("case_number") or pair["key"][0],
        "scenario_id": pair["scene_id"],
        "mode": pair["mode"],
        "canonical": _classify_field(baseline_eval, candidate_eval, "canonical_score", policy),
        "scenario": _classify_field(baseline_eval, candidate_eval, "scenario_score", policy),
    }
    # New hard gate failure always enters P2.
    if _new_gate_failure(baseline_eval, candidate_eval):
        result["new_hard_gate_failure"] = True
        result["classification"] = "p2"
    else:
        result["classification"] = _worst(
            result["canonical"]["classification"], result["scenario"]["classification"]
        )
    return result


def _classify_field(
    baseline_eval: dict[str, Any],
    candidate_eval: dict[str, Any],
    field: str,
    policy: dict[str, Any] | None,
) -> dict[str, Any]:
    baseline = baseline_eval.get(field)
    candidate = candidate_eval.get(field)
    delta = (
        round(candidate - baseline, 2) if baseline is not None and candidate is not None else None
    )
    return {
        "baseline": baseline,
        "candidate": candidate,
        "delta_points": delta,
        "classification": classify_delta(delta, policy),
    }


def _new_gate_failure(baseline_eval: dict[str, Any], candidate_eval: dict[str, Any]) -> bool:
    baseline_failed = bool(baseline_eval.get("applied_gate_ids")) and baseline_eval.get(
        "final_verdict"
    )
    candidate_failed = bool(candidate_eval.get("applied_gate_ids")) and candidate_eval.get(
        "final_verdict"
    )
    return (not baseline_failed) and candidate_failed


def _worst(*classifications: str) -> str:
    ranked = sorted(
        (c for c in classifications if c in CLASSIFICATION_ORDER),
        key=lambda c: CLASSIFICATION_ORDER[c],
    )
    if not ranked:
        return "unclassified"
    return ranked[-1]


def bucket_pairs(
    pairs: list[dict[str, Any]], policy: dict[str, Any] | None
) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {
        "p0": [],
        "p1": [],
        "p2": [],
        "unclassified": [],
    }
    for pair in pairs:
        classified = classify_pair(pair, policy)
        buckets[classified["classification"]].append(classified)
    return buckets
