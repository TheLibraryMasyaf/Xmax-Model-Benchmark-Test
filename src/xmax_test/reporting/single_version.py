"""Exact-batch single-version report generation."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable
from html import escape
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..errors import ContractError
from ..evaluation.aggregation import aggregate_evaluation_results
from ..evaluation.group_metrics import BatchReportingMetrics
from ..feedback.overrides import HumanOverrideService
from ..hashing import content_hash
from ..pipeline.manifests import frozen_evaluation_batch, frozen_run_batch
from ..time import utc_now


_P3_STANDARD_DEVIATION_THRESHOLD_POINTS = 10.0


class SingleVersionReportService:
    """Render one model version from one Run Batch and Evaluation Batch.

    The two manifests are mandatory. This prevents a report from silently
    mixing retries, historical benchmark versions, or another agent's batch.
    """

    def __init__(
        self,
        repository: Any,
        benchmark: dict[str, Any],
        scenario_pack: dict[str, Any],
    ) -> None:
        self._repository = repository
        self._benchmark = benchmark
        self._scenario_pack = scenario_pack
        self._overrides = HumanOverrideService(repository)

    def generate(
        self,
        *,
        report_id: str,
        model_version: str,
        run_batch_id: str,
        evaluation_batch_id: str,
        output_directory: str | Path,
        requested_scene_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        run_manifest = self._repository.get_batch_manifest("run_batch", run_batch_id)
        evaluation_manifest = self._repository.get_batch_manifest(
            "evaluation_batch", evaluation_batch_id
        )
        evaluations = {
            item["run_id"]: self._overrides.effective_result(item)
            for item in frozen_evaluation_batch(self._repository, evaluation_batch_id)
        }
        runs = sorted(
            frozen_run_batch(self._repository, run_batch_id),
            key=lambda item: _natural_key(
                str(item.get("case_number") or item.get("run_id") or "")
            ),
        )
        wrong_models = sorted(
            {str(run.get("model_id")) for run in runs if run.get("model_id") != model_version}
        )
        if wrong_models:
            raise ContractError(
                f"run batch {run_batch_id} contains models other than {model_version}: "
                + ", ".join(wrong_models)
            )
        completed_ids = {run["run_id"] for run in runs if run.get("status") == "completed"}
        extra_evaluations = sorted(set(evaluations) - completed_ids)
        if extra_evaluations:
            raise ContractError(
                "evaluation batch contains runs outside the selected completed run batch: "
                + ", ".join(extra_evaluations)
            )

        effective_results = list(evaluations.values())
        aggregate = aggregate_evaluation_results(effective_results)
        reporting_metrics = BatchReportingMetrics(self._repository).summarize(
            runs, effective_results
        )
        cases = [self._case_row(run, evaluations.get(run["run_id"])) for run in runs]
        reporting_metrics["P.3"]["diagnostic_stability"] = _repeat_stability_diagnostics(
            reporting_metrics["P.3"],
            cases,
            standard_deviation_threshold_points=_P3_STANDARD_DEVIATION_THRESHOLD_POINTS,
        )
        cases = _attach_repeat_stability(
            cases, reporting_metrics["P.3"]["diagnostic_stability"]
        )
        requirement_results = _evaluation_requirement_results(
            self._benchmark,
            runs,
            effective_results,
            reporting_metrics,
        )
        scenes = sorted({row["scenario_id"] for row in cases if row.get("scenario_id")})
        requested = requested_scene_ids or scenes
        missing_scenes = sorted(set(requested) - set(scenes))
        total_scores = [
            float(row["score_percent"])
            for row in cases
            if isinstance(row.get("score_percent"), (int, float))
        ]
        # Generation failures participate in every headline score as 0%. A
        # completed-but-not-evaluated run remains missing instead of being
        # silently converted to zero.
        canonical_scores = [
            float(row["canonical_score"])
            for row in cases
            if isinstance(row.get("canonical_score"), (int, float))
        ]
        scenario_scores = [
            float(row["scenario_score"])
            for row in cases
            if isinstance(row.get("scenario_score"), (int, float))
        ]
        dimension_names = {
            str(item.get("dimension_id")): str(item.get("name") or "")
            for item in self._benchmark.get("dimensions", [])
        }
        dimensions = sorted(
            (
                {
                    **item,
                    "dimension_name": dimension_names.get(
                        str(item.get("dimension_id") or ""), ""
                    ),
                    "standard_deviation_percent_points": _raw_score_sd_to_points(
                        item.get("standard_deviation")
                    ),
                }
                for item in aggregate["dimension_summary"]
            ),
            key=lambda item: _natural_key(item.get("dimension_id", "")),
        )
        criteria = sorted(
            (
                {
                    **item,
                    "standard_deviation_percent_points": _raw_score_sd_to_points(
                        item.get("standard_deviation")
                    ),
                }
                for item in aggregate["criterion_summary"]
            ),
            key=lambda item: _natural_key(item.get("criterion_id", "")),
        )
        priorities = _evidence_packets(cases, dimensions, criteria, reporting_metrics)
        credibility = _credibility(cases, evaluations, len(runs))
        report = {
            "report_schema_version": "single-version-report/1.2",
            "report_id": report_id,
            "status": (
                "complete"
                if not missing_scenes and len(evaluations) == len(completed_ids)
                else "partial"
            ),
            "generated_at": utc_now(),
            "model_version": model_version,
            "run_batch_id": run_batch_id,
            "evaluation_batch_id": evaluation_batch_id,
            "benchmark_version": self._benchmark.get("benchmark_version", ""),
            "scenario_pack_version": self._scenario_pack.get("version", ""),
            "requested_scene_ids": requested,
            "missing_scene_ids": missing_scenes,
            "coverage": {
                "run_count": len(runs),
                "completed_run_count": len(completed_ids),
                "generation_failure_count": sum(run.get("status") != "completed" for run in runs),
                "evaluated_completed_run_count": len(evaluations),
                "human_override_count": sum(
                    bool(item.get("human_override")) for item in effective_results
                ),
            },
            "scores": {
                "case_score_percent": _stats(total_scores),
                "canonical_score": _stats(canonical_scores),
                "scenario_score": _stats(scenario_scores),
            },
            "dimension_results": dimensions,
            "criterion_results": criteria,
            "reporting_metrics": reporting_metrics,
            "evaluation_requirement_results": requirement_results,
            "case_results": cases,
            "strengths": _rank(dimensions, reverse=True),
            "weaknesses": _rank(dimensions, reverse=False),
            "priorities": priorities,
            "credibility": credibility,
            "analysis_required": True,
            "audit": {
                "run_manifest_hash": run_manifest.get("content_hash"),
                "evaluation_manifest_hash": evaluation_manifest.get("content_hash"),
                "benchmark_hash": content_hash(self._benchmark),
                "scenario_pack_hash": content_hash(self._scenario_pack),
                "score_source": "effective criterion_results; generation errors are 0%",
            },
        }
        output = Path(output_directory)
        output.mkdir(parents=True, exist_ok=True)
        schema_path = (
            Path(__file__).resolve().parents[3] / "schemas" / "single-version-report.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(report),
            key=lambda error: list(error.path),
        )
        if errors:
            raise ContractError(f"single-version report schema error: {errors[0].message}")
        json_path = output / f"{report_id}.json"
        markdown_path = output / f"{report_id}.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        markdown_path.write_text(_render(report), encoding="utf-8")
        return {
            "report_id": report_id,
            "status": report["status"],
            "markdown_path": str(markdown_path),
            "json_path": str(json_path),
        }

    def _case_row(self, run: dict[str, Any], evaluation: dict[str, Any] | None) -> dict[str, Any]:
        case = self._repository.get_test_case(run["case_id"])
        failed = run.get("status") != "completed"
        score = (
            0.0
            if failed
            else (
                evaluation.get("effective_case_score_percent") if evaluation is not None else None
            )
        )
        return {
            "run_id": run["run_id"],
            "case_number": run.get("case_number", ""),
            "status": run.get("status"),
            "mode": run.get("mode"),
            "scenario_id": case.get("scenario_id"),
            "repeat_index": case.get("repeat_index"),
            "score_percent": score,
            "canonical_score": 0.0
            if failed
            else evaluation.get("canonical_score")
            if evaluation
            else None,
            "scenario_score": score,
            "evaluation_id": evaluation.get("evaluation_id") if evaluation else None,
            "gate_ids": evaluation.get("applied_gate_ids", []) if evaluation else [],
            "human_override": evaluation.get("human_override") if evaluation else None,
            "result_asset_id": run.get("result_asset_id"),
            "operation_recipe_id": case.get("operation_recipe_id"),
            "feed_asset_id": case.get("feed_asset_id"),
            "prompt_asset_ids": list(case.get("prompt_asset_ids", [])),
            "prompt_text": case.get("prompt_text", ""),
            "score_basis_signature": _score_basis_signature(evaluation),
        }


def _rank(items: list[dict[str, Any]], *, reverse: bool) -> list[dict[str, Any]]:
    scored = [item for item in items if isinstance(item.get("score_percent"), (int, float))]
    return sorted(
        scored,
        key=lambda item: (
            float(item["score_percent"]),
            _natural_key(item.get("dimension_id", "")),
        ),
        reverse=reverse,
    )[:5]


def _raw_score_sd_to_points(value: Any) -> float | None:
    """Convert a population SD on the 0–2 rubric to 0–100 percentage points."""

    if not isinstance(value, (int, float)):
        return None
    return round(float(value) / 2 * 100, 2)


def _score_basis_signature(evaluation: dict[str, Any] | None) -> str | None:
    """Identify the scoring denominator used by one evaluated run."""

    if not evaluation:
        return None
    weights = (evaluation.get("weight_resolution") or {}).get("effective_weights")
    criteria = evaluation.get("criterion_results")
    if not isinstance(weights, dict) or not isinstance(criteria, list):
        return None
    basis = {
        "effective_weights": weights,
        "criterion_applicability": sorted(
            (
                str(item.get("criterion_id") or ""),
                bool(item.get("applicable", True)),
            )
            for item in criteria
            if item.get("criterion_id")
        ),
    }
    return content_hash(basis)


def _evidence_packets(
    cases: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    criteria: list[dict[str, Any]],
    reporting_metrics: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    p0 = []
    failed = [item for item in cases if item.get("score_percent") == 0]
    gated = [item for item in cases if item.get("gate_ids")]
    if failed or gated:
        p0.append(
            {
                "issue": "存在生成失败、零分结果或硬门槛失败",
                "affected_runs": sorted(
                    {item["run_id"] for item in failed + gated}, key=_natural_key
                ),
                "analysis_status": "requires_agent_analysis",
            }
        )
    scored_criteria = sorted(
        (item for item in criteria if isinstance(item.get("score_percent"), (int, float))),
        key=lambda item: (
            float(item["score_percent"]),
            _natural_key(item.get("criterion_id", "")),
        ),
    )
    # BENCHMARK does not publish an absolute "bad" threshold. Only produce
    # relative improvement priorities when the batch actually has a score
    # spread; never invent a 60% pass line in report code.
    weak_criteria = (
        scored_criteria[: min(10, len(scored_criteria))]
        if scored_criteria
        and float(scored_criteria[0]["score_percent"]) < float(scored_criteria[-1]["score_percent"])
        else []
    )
    p1 = [
        {
            "issue": f"细则 {item['criterion_id']} 在本批次相对较低",
            "affected_criterion": item["criterion_id"],
            "score_percent": item["score_percent"],
            "analysis_status": "requires_agent_analysis",
        }
        for item in weak_criteria[:10]
    ]
    variable_dimensions = sorted(
        (
            item
            for item in dimensions
            if isinstance(item.get("standard_deviation"), (int, float))
            and float(item["standard_deviation"]) > 0
        ),
        key=lambda item: (
            -float(item["standard_deviation"]),
            _natural_key(item.get("dimension_id", "")),
        ),
    )
    p2 = [
        {
            "issue": f"维度 {item['dimension_id']} 在本批次波动较大",
            "affected_dimension": item["dimension_id"],
            "standard_deviation": item.get("standard_deviation"),
            "analysis_status": "requires_agent_analysis",
        }
        for item in variable_dimensions[:10]
    ]
    stability = reporting_metrics.get("P.3", {}).get("diagnostic_stability", {})
    for item in stability.get("unstable_groups", [])[:5]:
        p1.append(
            {
                "issue": f"重复组 {item['group_id']} 的同输入输出波动超过报告诊断界线",
                "affected_group": item["group_id"],
                "case_score_spread": item["case_score_spread"],
                "standard_deviation": item["standard_deviation"],
                "member_case_numbers": item["member_case_numbers"],
                "analysis_status": "requires_agent_analysis",
            }
        )
    return {"p0": p0, "p1": p1, "p2": p2}


def _repeat_stability_diagnostics(
    repeat_summary: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    standard_deviation_threshold_points: float,
) -> dict[str, Any]:
    """Add an explicit report-only interpretation to P.3 repeat facts.

    The Benchmark intentionally does not turn P.3 into a 0/1/2 score.  This
    diagnostic therefore keeps its own versioned threshold and exposes every
    denominator.  Population standard deviation is used because the frozen
    repetitions are the complete run set for this batch, not a sample estimate.
    """

    case_by_run = {item["run_id"]: item for item in cases}
    repeated = [
        item
        for item in repeat_summary.get("groups", [])
        if int(item.get("configured_repeat_count") or 0) >= 2
    ]
    classified: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    for group in repeated:
        stats = group.get("case_score_percent") or {}
        spread = group.get("case_score_spread")
        standard_deviation = stats.get("standard_deviation")
        if (
            not isinstance(standard_deviation, (int, float))
            or not isinstance(spread, (int, float))
            or int(stats.get("count") or 0) < 2
        ):
            continue
        member_rows = [
            case_by_run[run_id]
            for run_id in group.get("member_run_ids", [])
            if run_id in case_by_run
        ]
        base = {
            "group_id": group["group_id"],
            "mode": group.get("mode"),
            "scenario_id": group.get("scenario_id"),
            "member_run_ids": list(group.get("member_run_ids", [])),
            "member_case_numbers": sorted(
                (str(item.get("case_number") or "") for item in member_rows),
                key=_natural_key,
            ),
            "score_count": int(stats["count"]),
            "mean_score_percent": stats.get("mean"),
            "minimum_score_percent": stats.get("minimum"),
            "maximum_score_percent": stats.get("maximum"),
            "standard_deviation": standard_deviation,
            "case_score_spread": round(float(spread), 4),
        }
        basis_signatures = sorted(
            {
                str(item["score_basis_signature"])
                for item in member_rows
                if item.get("score_basis_signature")
            }
        )
        if not basis_signatures:
            unclassified.append(
                {
                    **base,
                    "classification": "basis_unavailable",
                    "unclassified_reason": "missing_score_basis",
                }
            )
            continue
        if len(basis_signatures) > 1:
            unclassified.append(
                {
                    **base,
                    "classification": "basis_mismatch",
                    "unclassified_reason": "inconsistent_score_basis",
                    "score_basis_variant_count": len(basis_signatures),
                }
            )
            continue
        classified.append(
            {
                **base,
                "classification": (
                    "unstable"
                    if float(standard_deviation) > standard_deviation_threshold_points
                    else "stable"
                ),
            }
        )

    stable = [item for item in classified if item["classification"] == "stable"]
    unstable = sorted(
        (item for item in classified if item["classification"] == "unstable"),
        key=lambda item: (-float(item["standard_deviation"]), item["group_id"]),
    )
    group_standard_deviations = [
        float(item["standard_deviation"])
        for item in classified
        if isinstance(item.get("standard_deviation"), (int, float))
    ]

    return {
        "status": "report_diagnostic_only_not_a_0_1_2_score",
        "policy_id": "repeat-score-population-sd-10-comparable-basis-v2",
        "standard_deviation_threshold_points": standard_deviation_threshold_points,
        "classification_definition": (
            "同一冻结重复组且评分口径一致时，Case总分总体标准差大于阈值为不稳定；"
            "小于等于阈值为稳定"
        ),
        "standard_deviation_definition": (
            "评分口径一致的组内所有已评分独立Run之Case总分总体标准差"
            "（population standard deviation，单位：百分点）"
        ),
        "interpretation": "稳定只表示重复一致，不表示质量高；低分结果也可能稳定复现。",
        "classified_group_count": len(classified),
        "unclassified_group_count": len(repeated) - len(classified),
        "basis_mismatch_group_count": sum(
            item["classification"] == "basis_mismatch" for item in unclassified
        ),
        "basis_unavailable_group_count": sum(
            item["classification"] == "basis_unavailable" for item in unclassified
        ),
        "stable_group_count": len(stable),
        "unstable_group_count": len(unstable),
        "unstable_group_rate_percent": _rate(len(unstable), len(classified)),
        "group_standard_deviation": _stats(group_standard_deviations),
        "stable_groups": stable,
        "unstable_groups": unstable,
        "unclassified_groups": unclassified,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


def _attach_repeat_stability(
    cases: list[dict[str, Any]], diagnostic: dict[str, Any]
) -> list[dict[str, Any]]:
    by_run: dict[str, dict[str, Any]] = {}
    groups = (
        diagnostic.get("stable_groups", [])
        + diagnostic.get("unstable_groups", [])
        + diagnostic.get("unclassified_groups", [])
    )
    for group in groups:
        for run_id in group.get("member_run_ids", []):
            by_run[str(run_id)] = group
    output = []
    for item in cases:
        group = by_run.get(str(item.get("run_id") or ""))
        output.append(
            {
                **item,
                "repeat_group_id": group.get("group_id") if group else None,
                "repeat_group_standard_deviation": (
                    group.get("standard_deviation") if group else None
                ),
                "repeat_group_stability": group.get("classification") if group else None,
            }
        )
    return output


def _evaluation_requirement_results(
    benchmark: dict[str, Any],
    runs: list[dict[str, Any]],
    results: list[dict[str, Any]],
    reporting_metrics: dict[str, Any],
) -> dict[str, Any]:
    """Enumerate every Benchmark scoring criterion and report-only metric.

    A requirement is never dropped merely because it is inapplicable,
    unassessable, uncovered, or represented by a non-score metric.
    """

    mode_by_run = {str(run["run_id"]): str(run.get("mode") or "") for run in runs}
    result_by_mode: dict[str, list[dict[str, Any]]] = {"offline": [], "realtime": []}
    for result in results:
        mode = mode_by_run.get(str(result.get("run_id") or ""))
        if mode in result_by_mode:
            result_by_mode[mode].append(result)

    scoring_rows = []
    for dimension in benchmark.get("dimensions", []):
        eligible_modes = _eligible_modes(dimension.get("applicable_modes", []))
        eligible_results = [
            result for mode in eligible_modes for result in result_by_mode.get(mode, [])
        ]
        for criterion in dimension.get("criteria", []):
            criterion_id = criterion.get("criterion_id")
            observed = [
                item
                for result in eligible_results
                for item in result.get("criterion_results", [])
                if item.get("criterion_id") == criterion_id
            ]
            applicable = [item for item in observed if item.get("applicable", True)]
            assessed = [
                item for item in applicable if isinstance(item.get("score"), (int, float))
            ]
            not_applicable = [item for item in observed if not item.get("applicable", True)]
            unassessable = [
                item for item in applicable if not isinstance(item.get("score"), (int, float))
            ]
            uncovered_count = max(0, len(eligible_results) - len(observed))
            if not eligible_results:
                status = "inapplicable_to_batch"
            elif assessed and not unassessable and not uncovered_count:
                status = "scored"
            elif assessed:
                status = "partially_scored"
            elif applicable:
                status = "unassessable"
            elif observed and len(not_applicable) == len(observed) and not uncovered_count:
                status = "not_applicable"
            else:
                status = "uncovered"
            scores = [float(item["score"]) for item in assessed]
            scoring_rows.append(
                {
                    "requirement_id": criterion_id,
                    "requirement_name": criterion.get("name", ""),
                    "dimension_id": dimension.get("dimension_id"),
                    "dimension_name": dimension.get("name", ""),
                    "measurement_type": "score_0_1_2",
                    "applicable_modes": sorted(eligible_modes),
                    "status": status,
                    "eligible_result_count": len(eligible_results),
                    "observed_result_count": len(observed),
                    "assessable_count": len(assessed),
                    "unassessable_count": len(unassessable),
                    "not_applicable_count": len(not_applicable),
                    "uncovered_count": uncovered_count,
                    "score_percent": (
                        round(statistics.mean(scores) / 2 * 100, 2) if scores else None
                    ),
                }
            )

    metric_rows = []
    run_modes = {str(run.get("mode") or "") for run in runs}
    for metric in benchmark.get("reporting_metrics", []):
        metric_id = str(metric.get("metric_id") or "")
        eligible_modes = _eligible_modes(metric.get("applicable_modes", []))
        applicable = bool(run_modes & eligible_modes)
        payload = reporting_metrics.get(metric_id)
        status = "reported" if applicable and isinstance(payload, dict) else "uncovered"
        if not applicable:
            status = "inapplicable_to_batch"
        elif metric_id == "P.4" and not int(
            (payload or {}).get("model_generation_elapsed_s", {}).get("observed_count") or 0
        ):
            status = "reported_no_separated_timing_evidence"
        elif metric_id == "RP.2" and not int((payload or {}).get("perturbation_run_count") or 0):
            status = "reported_no_perturbation_evidence"
        elif metric_id == "RP.3" and not int(
            (payload or {}).get("event_to_output_p95_ms", {}).get("observed_count") or 0
        ):
            status = "reported_no_continuous_latency_evidence"
        metric_rows.append(
            {
                "requirement_id": metric_id,
                "requirement_name": metric.get("name", ""),
                "measurement_type": "batch_or_runtime_metric",
                "applicable_modes": sorted(eligible_modes),
                "status": status,
                "result_summary": _reporting_metric_result_summary(metric_id, payload or {}),
            }
        )

    return {
        "policy": (
            "Benchmark中的每条评价要求均必须列出：0/1/2评分细则统一进入细则结果表，"
            "非评分批次/运行指标单独列出；无结果时显式标为不适用、不可评或未覆盖。"
        ),
        "scoring_criteria": scoring_rows,
        "reporting_metrics": metric_rows,
        "requirement_count": len(scoring_rows) + len(metric_rows),
    }


def _eligible_modes(applicable_modes: list[Any]) -> set[str]:
    modes = {str(item) for item in applicable_modes}
    return {"offline", "realtime"} if "both" in modes else modes & {"offline", "realtime"}


def _reporting_metric_result_summary(metric_id: str, payload: dict[str, Any]) -> str:
    if metric_id == "P.2":
        return (
            f"计划{payload.get('planned_run_count', 0)}，完成{payload.get('completed_run_count', 0)}，"
            f"有效{payload.get('valid_result_count', 0)}，无效{payload.get('invalid_output_count', 0)}，"
            f"失败{payload.get('generation_failure_count', 0)}，重试{payload.get('retry_count', 0)}"
        )
    if metric_id == "P.3":
        stability = payload.get("diagnostic_stability", {})
        return (
            f"可判定{stability.get('classified_group_count', 0)}组，"
            f"稳定{stability.get('stable_group_count', 0)}组，"
            f"不稳定{stability.get('unstable_group_count', 0)}组，"
            f"不稳定率{_pct(stability.get('unstable_group_rate_percent'))}，"
            f"评分口径不一致{stability.get('basis_mismatch_group_count', 0)}组"
        )
    if metric_id == "P.4":
        return (
            f"离线Run {payload.get('run_count', 0)}，"
            f"模型生成耗时观测{payload.get('model_generation_elapsed_s', {}).get('observed_count', 0)}，"
            f"端到端交付观测{payload.get('end_to_end_delivery_elapsed_s', {}).get('observed_count', 0)}，"
            f"RTF观测{payload.get('generation_rtf', {}).get('count', 0)}"
        )
    if metric_id == "RP.1":
        return (
            f"实时Run {payload.get('run_count', 0)}，"
            f"首帧观测{payload.get('first_frame_ms', {}).get('observed_count', 0)}，"
            f"FPS观测{payload.get('fps', {}).get('observed_count', 0)}"
        )
    if metric_id == "RP.2":
        return (
            f"实时Run {payload.get('run_count', 0)}，异常实验"
            f"{payload.get('perturbation_run_count', 0)}，恢复"
            f"{payload.get('recovered_run_count', 0)}"
        )
    if metric_id == "RP.3":
        return (
            f"实时Run {payload.get('run_count', 0)}，"
            f"持续时延P95观测{payload.get('event_to_output_p95_ms', {}).get('observed_count', 0)}，"
            f"网络Profile {len(payload.get('network_profile_ids', []))}个"
        )
    return "已取得结构化指标" if payload else "未取得指标"


def _credibility(
    cases: list[dict[str, Any]],
    evaluations: dict[str, dict[str, Any]],
    run_count: int,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    completed = [item for item in cases if item.get("status") == "completed"]
    if len(evaluations) != len(completed):
        issues.append(
            {
                "code": "incomplete_evaluation_coverage",
                "severity": "error",
                "description": "完成的Run未全部进入所选评测批次。",
            }
        )
    scenarios = {item.get("scenario_id") for item in cases if item.get("scenario_id")}
    recipes = {
        item.get("operation_recipe_id") for item in cases if item.get("operation_recipe_id")
    }
    if run_count >= 10 and len(recipes) > 1 and len(scenarios) <= 1:
        issues.append(
            {
                "code": "scenario_assignment_degenerate",
                "severity": "error",
                "description": "多个玩法配方被压缩到同一个场景，场景权重结果仅可诊断使用。",
            }
        )
    issues.append(
        {
            "code": "automated_judges_only",
            "severity": "warning",
            "description": "本报告未包含独立人工盲评校准，结论应结合抽样复核。",
        }
    )
    unique = {(item["code"], item["description"]): item for item in issues}
    issues = list(unique.values())
    return {
        "status": (
            "diagnostic"
            if any(item["severity"] == "error" for item in issues)
            else "credible_with_warnings"
        ),
        "issues": issues,
    }


def _expected_group_members(
    cases: list[dict[str, Any]],
    target: dict[str, Any] | None,
    scope: str,
) -> set[str]:
    if target is None:
        return set()

    def repeat_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("mode"),
            item.get("feed_asset_id"),
            tuple(item.get("prompt_asset_ids", [])),
            item.get("prompt_text", ""),
            item.get("operation_recipe_id"),
        )

    def transfer_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("mode"),
            tuple(item.get("prompt_asset_ids", [])),
            item.get("prompt_text", ""),
            item.get("operation_recipe_id"),
            item.get("scenario_id"),
        )

    if scope == "batch":
        return {
            item["run_id"] for item in cases if item.get("mode") == target.get("mode")
        }
    key_function = repeat_key if scope == "repeat_group" else transfer_key
    target_key = key_function(target)
    return {item["run_id"] for item in cases if key_function(item) == target_key}


def _natural_key(value: Any) -> tuple[Any, ...]:
    import re

    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", str(value))
    )


def _stats(values: Iterable[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            key: None
            for key in (
                "mean",
                "median",
                "minimum",
                "maximum",
                "standard_deviation",
                "p25",
                "p75",
            )
        } | {"count": 0}
    return {
        "count": len(ordered),
        "mean": round(statistics.mean(ordered), 2),
        "median": round(statistics.median(ordered), 2),
        "minimum": round(min(ordered), 2),
        "maximum": round(max(ordered), 2),
        "standard_deviation": round(statistics.pstdev(ordered), 2),
        "p25": round(_percentile(ordered, 0.25), 2),
        "p75": round(_percentile(ordered, 0.75), 2),
    }


def _percentile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _render(report: dict[str, Any]) -> str:
    coverage = report["coverage"]
    scores = report["scores"]
    stability = report["reporting_metrics"]["P.3"].get("diagnostic_stability", {})
    group_sd = stability.get("group_standard_deviation", {})
    lines = [
        f"# XMAX {report['model_version']} 单版本评测报告",
        "",
        f"- 报告ID：`{report['report_id']}`",
        f"- 完整性状态：`{report['status']}`",
        f"- Run批次：`{report['run_batch_id']}`",
        f"- 评测批次：`{report['evaluation_batch_id']}`",
        f"- Benchmark：`{report['benchmark_version']}`",
        "",
        "## 摘要",
        "",
        f"- 总分：{_format_stats(scores['scenario_score'])}",
        f"- 覆盖：Run {coverage['run_count']}；完成 {coverage['completed_run_count']}；生成失败 {coverage['generation_failure_count']}；已评测 {coverage['evaluated_completed_run_count']}；人工修订 {coverage['human_override_count']}",
        "- 可信度：自动Judge结果，尚未进行独立人工盲评校准。",
        "",
        "## P.3 同输入重复稳定性",
        "",
        f"- 组内总分标准差：均值 {_value(group_sd.get('mean'))}，中位数 {_value(group_sd.get('median'))}，范围 {_value(group_sd.get('minimum'))}–{_value(group_sd.get('maximum'))} 个百分点；标准差>{_value(stability.get('standard_deviation_threshold_points'))}个百分点判为不稳定。",
        f"- 仅判定同输入且评分口径一致的重复组；口径不一致 {stability.get('basis_mismatch_group_count', 0)} 组。稳定性不代表质量高。",
    ]
    lines.extend(
        [
        "",
            "## 批次与运行指标",
            "",
            "| 指标 | 状态 | 本批次结果 |",
            "| --- | --- | --- |",
        ]
    )
    for item in report["evaluation_requirement_results"]["reporting_metrics"]:
        lines.append(
            f"| {item['requirement_id']} {item.get('requirement_name', '')} | {_requirement_status_label(item['status'])} | {item['result_summary']} |"
        )
    lines.extend(
        [
        "",
        "## 维度结果",
        "",
        "| 维度 | 分数 | 覆盖 | 标准差（百分点） |",
        "| --- | ---: | ---: | ---: |",
        ]
    )
    for item in report["dimension_results"]:
        lines.append(
            f"| {item['dimension_id']} {item.get('dimension_name', '')} | {_pct(item.get('score_percent'))} | {item.get('assessable_count', 0)}/{item.get('total_evaluations', 0)} | {_value(item.get('standard_deviation_percent_points'))} |"
        )
    lines.extend(
        [
            "",
            "## 细则结果",
            "",
            "| 维度 | 细则 | 状态 | 得分 | 可评/应出现 | 不可评 | 不适用 | 未覆盖 | 标准差（百分点） | 分布 0/(0,1)/1/(1,2)/2 |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    criterion_summary = {
        item["criterion_id"]: item for item in report["criterion_results"]
    }
    for item in report["evaluation_requirement_results"]["scoring_criteria"]:
        summary = criterion_summary.get(item["requirement_id"], {})
        distribution = summary.get("score_distribution") or {}
        values = "/".join(
            str(distribution.get(key, 0)) for key in ("0", "0_to_1", "1", "1_to_2", "2")
        )
        lines.append(
            f"| {item.get('dimension_id', '')} {item.get('dimension_name', '')} | {item['requirement_id']} {item.get('requirement_name', '')} | {_requirement_status_label(item['status'])} | {_pct(item.get('score_percent'))} | {item['assessable_count']}/{item['eligible_result_count']} | {item['unassessable_count']} | {item['not_applicable_count']} | {item['uncovered_count']} | {_value(summary.get('standard_deviation_percent_points'))} | {values} |"
        )
    for priority in ("p0", "p1", "p2"):
        lines.extend(["", f"## {priority.upper()} 证据包（待Agent分析）", ""])
        items = report["priorities"][priority]
        if not items:
            lines.append("- 本批次没有形成该级别的证据包。")
        else:
            for item in items:
                affected = (
                    item.get("affected_criterion")
                    or item.get("affected_dimension")
                    or item.get("affected_group")
                    or ", ".join(item.get("affected_runs", []))
                )
                lines.append(f"- {item['issue']}。受影响对象：{affected}")
    lines.extend(
        [
            "",
            "## 逐Case结果",
            "",
            "<table>",
            "<thead><tr><th>Case</th><th>Run</th><th>状态</th><th>场景</th><th>重复序号</th><th>分数</th><th>组内总分标准差（百分点）</th><th>稳定性</th><th>Evaluation</th></tr></thead>",
            "<tbody>",
        ]
    )
    for group in _case_display_groups(report["case_results"]):
        rowspan = len(group)
        for index, item in enumerate(group):
            cells = [
                f"<td>{escape(str(item['case_number']))}</td>",
                f"<td>{escape(str(item['run_id']))}</td>",
                f"<td>{escape(str(item['status']))}</td>",
                f"<td>{escape(str(item.get('scenario_id') or ''))}</td>",
                f"<td>{escape(str(item.get('repeat_index') or ''))}</td>",
                f"<td>{_pct(item.get('score_percent'))}</td>",
            ]
            if index == 0:
                stability_label = {
                    "stable": "稳定",
                    "unstable": "不稳定",
                    "basis_mismatch": "评分口径不一致",
                    "basis_unavailable": "缺少评分口径",
                }.get(item.get("repeat_group_stability"), "不可判定")
                cells.extend(
                    [
                        f'<td rowspan="{rowspan}">{_value(item.get("repeat_group_standard_deviation"))}</td>',
                        f'<td rowspan="{rowspan}">{stability_label}</td>',
                    ]
                )
            cells.append(
                f"<td>{escape(str(item.get('evaluation_id') or 'not evaluated'))}</td>"
            )
            lines.append("<tr>" + "".join(cells) + "</tr>")
    lines.extend(["</tbody>", "</table>"])
    lines.extend(
        [
            "",
            "## 审计信息",
            "",
            "```json",
            json.dumps(report["audit"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _case_display_groups(cases: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Keep first-seen group order while making rowspan members contiguous."""

    members: dict[str, list[dict[str, Any]]] = {}
    for item in cases:
        group_id = item.get("repeat_group_id")
        if group_id:
            members.setdefault(str(group_id), []).append(item)
    seen: set[str] = set()
    output: list[list[dict[str, Any]]] = []
    for item in cases:
        group_id = item.get("repeat_group_id")
        if not group_id:
            output.append([item])
            continue
        key = str(group_id)
        if key not in seen:
            output.append(members[key])
            seen.add(key)
    return output


def _requirement_status_label(status: Any) -> str:
    return {
        "scored": "已评分",
        "partially_scored": "部分评分",
        "not_applicable": "不适用",
        "inapplicable_to_batch": "本批不适用",
        "unassessable": "不可评",
        "uncovered": "未覆盖",
        "reported": "已报告",
        "reported_no_perturbation_evidence": "已报告（未执行异常实验）",
    }.get(str(status), str(status))


def _format_stats(stats: dict[str, Any]) -> str:
    if not stats.get("count"):
        return "不可评"
    return (
        f"均值 {_pct(stats['mean'])}，中位数 {_pct(stats['median'])}，"
        f"最小/最大 {_pct(stats['minimum'])}/{_pct(stats['maximum'])}，"
        f"标准差 {_value(stats['standard_deviation'])} 个百分点，n={stats['count']}"
    )


def _pct(value: Any) -> str:
    return "不可评" if not isinstance(value, (int, float)) else f"{float(value):.2f}%"


def _value(value: Any) -> str:
    return "不可评" if not isinstance(value, (int, float)) else f"{float(value):.4f}"
