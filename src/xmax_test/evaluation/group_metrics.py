"""Deterministic batch-report metrics for P.2/P.3 and RP.1/RP.2.

These facts are calculated from one frozen Run Batch plus its Evaluation
Results.  They never emit Judgments and therefore can never change a
single-video criterion, dimension or weighted score.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any

REPORTING_METRIC_SCOPES = {
    "P.2": "frozen_run_batch",
    "P.3": "repeat_group",
    "RP.1": "frozen_realtime_batch",
    "RP.2": "frozen_realtime_batch",
}


class BatchReportingMetrics:
    """Summarize model/runtime performance without manufacturing 0/1/2 scores."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def summarize(
        self,
        runs: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        run_ids = [str(run.get("run_id") or "") for run in runs]
        if not all(run_ids) or len(run_ids) != len(set(run_ids)):
            raise ValueError("batch reporting requires unique non-empty frozen run IDs")
        result_run_ids = [str(result.get("run_id") or "") for result in results]
        if len(result_run_ids) != len(set(result_run_ids)):
            raise ValueError("batch reporting received duplicate results for one Run")
        outside = sorted(set(result_run_ids) - set(run_ids))
        if outside:
            raise ValueError(
                "batch reporting results are outside the frozen Run set: " + ", ".join(outside)
            )

        cases = {
            run["run_id"]: self._repository.get_test_case(run["case_id"])
            for run in runs
            if run.get("case_id")
        }
        by_run = {item["run_id"]: item for item in results}
        return {
            "summary_version": "xmax-batch-reporting-metrics/1.0",
            "P.2": self._generation_summary(runs, by_run),
            "P.3": self._repeat_summary(runs, cases, by_run),
            "RP.1": self._realtime_delivery_summary(runs),
            "RP.2": self._realtime_recovery_summary(runs),
        }

    @staticmethod
    def _generation_summary(
        runs: list[dict[str, Any]], results: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        completed = [item for item in runs if item.get("status") == "completed"]
        returned = [item for item in completed if item.get("result_asset_id")]
        validity = {
            run_id: _criterion_score(result, "P.1") for run_id, result in results.items()
        }
        valid = [
            item
            for item in returned
            if _is_positive_score(validity.get(item["run_id"]))
            and not _is_invalid_result(results.get(item["run_id"], {}))
        ]
        invalid = [
            item
            for item in returned
            if _is_invalid_result(results.get(item["run_id"], {}))
            or validity.get(item["run_id"]) == 0
        ]
        retries = sum(int(item.get("metrics", {}).get("retry_count") or 0) for item in runs)
        failure_reasons = Counter(
            str(
                item.get("error", {}).get("code")
                or item.get("metrics", {}).get("failure_reason")
                or item.get("status")
            )
            for item in runs
            if item.get("status") != "completed"
        )
        elapsed = [
            float(item["metrics"]["generation_elapsed_s"])
            for item in runs
            if isinstance(item.get("metrics", {}).get("generation_elapsed_s"), (int, float))
        ]
        total = len(runs)
        return {
            "scope": REPORTING_METRIC_SCOPES["P.2"],
            "planned_run_count": total,
            "completed_run_count": len(completed),
            "result_return_count": len(returned),
            "valid_result_count": len(valid),
            "invalid_output_count": len(invalid),
            "validity_unassessed_count": max(0, len(returned) - len(valid) - len(invalid)),
            "generation_failure_count": total - len(completed),
            "first_attempt_completed_count": sum(
                item.get("status") == "completed"
                and int(item.get("metrics", {}).get("retry_count") or 0) == 0
                for item in runs
            ),
            "retry_count": retries,
            "completion_rate_percent": _percent(len(completed), total),
            "valid_result_rate_percent": _percent(len(valid), total),
            "invalid_output_rate_percent": _percent(len(invalid), total),
            "failure_reason_counts": dict(sorted(failure_reasons.items())),
            "generation_elapsed_s": _stats(elapsed),
        }

    @staticmethod
    def _repeat_summary(
        runs: list[dict[str, Any]],
        cases: dict[str, dict[str, Any]],
        results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for run in runs:
            case = cases.get(run.get("run_id"))
            if case:
                groups[_repeat_key(run, case)].append(run)
        rows = []
        for key, members in sorted(groups.items(), key=lambda item: repr(item[0])):
            completed = [item for item in members if item.get("status") == "completed"]
            valid_count = sum(
                _is_positive_score(
                    _criterion_score(results.get(item["run_id"], {}), "P.1")
                )
                and not _is_invalid_result(results.get(item["run_id"], {}))
                for item in completed
            )
            scores = [
                float(results[item["run_id"]]["case_score_percent"])
                for item in completed
                if item.get("run_id") in results
                and isinstance(results[item["run_id"]].get("case_score_percent"), (int, float))
            ]
            rows.append(
                {
                    "group_id": _group_id(key),
                    "model_id": key[0],
                    "mode": key[1],
                    "scenario_id": key[-1],
                    "configured_repeat_count": len(members),
                    "member_run_ids": sorted(item["run_id"] for item in members),
                    "completed_count": len(completed),
                    "valid_result_count": valid_count,
                    "completion_rate_percent": _percent(len(completed), len(members)),
                    "valid_result_rate_percent": _percent(valid_count, len(members)),
                    "case_score_percent": _stats(scores),
                    "case_score_spread": round(max(scores) - min(scores), 4)
                    if len(scores) >= 2
                    else None,
                }
            )
        repeated = [item for item in rows if item["configured_repeat_count"] >= 2]
        return {
            "scope": REPORTING_METRIC_SCOPES["P.3"],
            "repeat_count_source": "frozen_run_request",
            "group_count": len(rows),
            "repeated_group_count": len(repeated),
            "groups": rows,
        }

    @staticmethod
    def _realtime_delivery_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
        realtime = [item for item in runs if item.get("mode") == "realtime"]
        return {
            "scope": REPORTING_METRIC_SCOPES["RP.1"],
            "run_count": len(realtime),
            "connect_ms": _metric_stats(realtime, "connect_ms"),
            "first_frame_ms": _metric_stats(realtime, "first_frame_ms"),
            "first_valid_result_ms": _metric_stats(realtime, "first_valid_result_ms"),
            "fps": _metric_stats(realtime, "fps"),
            "dropped_frames": _metric_stats(realtime, "dropped_frames"),
            "duplicate_frame_ratio": _metric_stats(realtime, "duplicate_frame_ratio"),
            "freeze_duration_ms": _metric_stats(realtime, "freeze_duration_ms"),
        }

    @staticmethod
    def _realtime_recovery_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
        realtime = [item for item in runs if item.get("mode") == "realtime"]
        perturbations = [
            item for item in realtime if int(item.get("metrics", {}).get("perturbation_count") or 0)
        ]
        recovered = [
            item for item in perturbations if isinstance(item.get("metrics", {}).get("recovery_ms"), (int, float))
        ]
        return {
            "scope": REPORTING_METRIC_SCOPES["RP.2"],
            "run_count": len(realtime),
            "perturbation_run_count": len(perturbations),
            "recovered_run_count": len(recovered),
            "automatic_recovery_rate_percent": _percent(len(recovered), len(perturbations)),
            "recovery_ms": _metric_stats(recovered, "recovery_ms"),
            "session_duration_s": _metric_stats(realtime, "session_duration_s"),
            "fps_window_cv": _metric_stats(realtime, "fps_window_cv"),
            "latency_window_cv": _metric_stats(realtime, "latency_window_cv"),
            "quality_window_delta": _metric_stats(realtime, "quality_window_delta"),
        }


def _criterion_score(result: dict[str, Any], criterion_id: str) -> float | None:
    for item in result.get("criterion_results", []):
        if item.get("criterion_id") == criterion_id and isinstance(item.get("score"), (int, float)):
            return float(item["score"])
    return None


def _is_positive_score(score: float | None) -> bool:
    """P.1 is a hard gate: only zero is invalid, positive consensus is valid."""

    return isinstance(score, (int, float)) and float(score) > 0


def _is_invalid_result(result: dict[str, Any]) -> bool:
    return result.get("final_verdict") == "invalid_result" or bool(
        result.get("applied_gate_ids")
    )


def _repeat_key(run: dict[str, Any], case: dict[str, Any]) -> tuple[Any, ...]:
    return (
        run.get("model_id"),
        run.get("mode"),
        case.get("feed_asset_id"),
        tuple(case.get("prompt_asset_ids", [])),
        case.get("prompt_text", ""),
        case.get("operation_recipe_id"),
        case.get("generation_config_hash") or _config_hash(case.get("generation_config", {})),
        case.get("scenario_id"),
    )


def _group_id(key: tuple[Any, ...]) -> str:
    from ..hashing import content_hash

    return f"repeat-{content_hash(list(key))[:12]}"


def _config_hash(config: dict[str, Any]) -> str:
    from ..hashing import content_hash

    return content_hash(config)


def _metric_stats(runs: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [
        float(item["metrics"][key])
        for item in runs
        if isinstance(item.get("metrics", {}).get(key), (int, float))
    ]
    return {"observed_count": len(values), "run_count": len(runs), **_stats(values)}


def _percent(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "minimum": None, "maximum": None, "standard_deviation": None, "p50": None, "p95": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": round(statistics.mean(ordered), 4),
        "median": round(statistics.median(ordered), 4),
        "minimum": round(min(ordered), 4),
        "maximum": round(max(ordered), 4),
        "standard_deviation": round(statistics.pstdev(ordered), 4),
        "p50": round(_percentile(ordered, 0.5), 4),
        "p95": round(_percentile(ordered, 0.95), 4),
    }


def _percentile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
