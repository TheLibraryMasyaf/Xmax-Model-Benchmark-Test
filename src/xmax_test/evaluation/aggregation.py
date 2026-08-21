"""Deterministic batch summaries derived from persisted per-video results."""

from __future__ import annotations

import math
import statistics
from typing import Any


def aggregate_evaluation_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate cases at criterion, dimension and total-score levels.

    The function never reads raw Judge dimension scores. Inputs must already be
    fused EvaluationResults whose criterion rows are the source of truth.
    """

    return {
        "summary_version": "criterion-batch-summary/1.0",
        "evaluation_count": len(results),
        "criterion_summary": _aggregate_items(results, "criterion_results", "criterion_id"),
        "dimension_summary": _aggregate_items(results, "dimension_results", "dimension_id"),
        "case_score_summary": _stats(
            [
                float(item["case_score_percent"])
                for item in results
                if isinstance(item.get("case_score_percent"), (int, float))
            ],
            total=len(results),
            scale="percent",
        ),
    }


def _aggregate_items(
    results: list[dict[str, Any]], collection: str, identifier: str
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        for item in result.get(collection, []):
            item_id = item.get(identifier)
            if item_id:
                grouped.setdefault(item_id, []).append(item)
    output: list[dict[str, Any]] = []
    for item_id, items in sorted(grouped.items()):
        scores = [
            float(item["score"]) for item in items if isinstance(item.get("score"), (int, float))
        ]
        row = {
            identifier: item_id,
            "total_evaluations": len(items),
            **_stats(scores, total=len(items), scale="raw_0_2"),
        }
        first = items[0]
        if first.get("dimension_id"):
            row["dimension_id"] = first["dimension_id"]
        if first.get("criterion_name"):
            row["criterion_name"] = first["criterion_name"]
        output.append(row)
    return output


def _stats(values: list[float], *, total: int, scale: str) -> dict[str, Any]:
    count = len(values)
    if not values:
        return {
            "assessable_count": 0,
            "unassessable_count": total,
            "coverage_percent": 0.0,
            "mean_score": None,
            "score_percent": None,
            "median": None,
            "minimum": None,
            "maximum": None,
            "standard_deviation": None,
            "p25": None,
            "p75": None,
            "score_distribution": _distribution([]) if scale == "raw_0_2" else None,
        }
    ordered = sorted(values)
    mean = sum(values) / count
    return {
        "assessable_count": count,
        "unassessable_count": max(0, total - count),
        "coverage_percent": round(count / total * 100, 2) if total else 0.0,
        "mean_score": round(mean, 4),
        "score_percent": round(mean / 2 * 100, 2) if scale == "raw_0_2" else round(mean, 2),
        "median": round(statistics.median(values), 4),
        "minimum": round(min(values), 4),
        "maximum": round(max(values), 4),
        "standard_deviation": round(statistics.pstdev(values), 4),
        "p25": round(_percentile(ordered, 0.25), 4),
        "p75": round(_percentile(ordered, 0.75), 4),
        "score_distribution": _distribution(values) if scale == "raw_0_2" else None,
    }


def _percentile(ordered: list[float], fraction: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _distribution(values: list[float]) -> dict[str, int]:
    buckets = {"0": 0, "0_to_1": 0, "1": 0, "1_to_2": 0, "2": 0}
    for value in values:
        if math.isclose(value, 0.0):
            buckets["0"] += 1
        elif value < 1.0:
            buckets["0_to_1"] += 1
        elif math.isclose(value, 1.0):
            buckets["1"] += 1
        elif value < 2.0:
            buckets["1_to_2"] += 1
        else:
            buckets["2"] += 1
    return buckets
